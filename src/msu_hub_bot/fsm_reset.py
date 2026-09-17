"""Explicit conversation reset for an operator who has stopped every poller."""

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Never, cast

from redis.asyncio import Redis

from msu_hub_bot.telegram.storage import reset_legacy_fsm, reset_v3_fsm

Generation = Literal["legacy", "v3"]
_CONFIG_KEYS = ("HUB_NAME", "HUB_REDIS_HOST", "HUB_REDIS_PORT", "HUB_REDIS_PASSWORD", "HUB_REDIS_DB")


@dataclass(frozen=True, slots=True, repr=False)
class ResetConfig:
    namespace: str
    host: str
    port: int = 6379
    password: str = ""
    database: int = 0

    def __post_init__(self) -> None:
        if (
            not self.namespace
            or any(character in self.namespace for character in "*?[]\\")
            or any(character.isspace() for character in self.namespace)
        ):
            raise ValueError("FSM reset requires a literal nonempty namespace")
        if not self.host.strip() or not 1 <= self.port <= 65535 or self.database < 0:
            raise ValueError("Invalid FSM reset Redis configuration")


def load_config(environment: Mapping[str, str]) -> ResetConfig:
    """Read only maintenance settings; explicit variables override the envelope."""
    values: dict[str, str] = {}
    if payload := environment.get("HUB_CONFIG_JSON"):
        envelope: object = json.loads(payload)
        if not isinstance(envelope, dict) or any(
            not isinstance(key, str)
            or not (re.fullmatch(r"HUB_[A-Z0-9_]+", key) or key == "LOGFIRE_TOKEN")
            or key == "HUB_CONFIG_JSON"
            or not isinstance(value, str)
            or "\0" in value
            for key, value in envelope.items()
        ):
            raise ValueError("Invalid deployment configuration envelope")
        values.update({key: envelope[key] for key in _CONFIG_KEYS if key in envelope})
    values.update({key: environment[key] for key in _CONFIG_KEYS if key in environment})
    return ResetConfig(
        namespace=values.get("HUB_NAME", "hub"),
        host=values.get("HUB_REDIS_HOST", ""),
        port=int(values.get("HUB_REDIS_PORT", "6379")),
        password=values.get("HUB_REDIS_PASSWORD", ""),
        database=int(values.get("HUB_REDIS_DB", "0")),
    )


async def reset(config: ResetConfig, generation: Generation) -> int:
    if generation not in ("legacy", "v3"):
        raise ValueError("Invalid FSM generation")
    client = Redis(
        host=config.host,
        port=config.port,
        password=config.password or None,
        db=config.database,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=10,
    )
    try:
        async with asyncio.timeout(60):
            operation = reset_legacy_fsm if generation == "legacy" else reset_v3_fsm
            return await operation(client, prefix=config.namespace)
    finally:
        await client.aclose()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # Arguments can be pasted accidentally; never echo an unknown value.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid FSM reset arguments; use --help.\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description="Delete only the selected generation of FSM state/data. Stop all pollers first.")
    parser.add_argument("--generation", choices=("legacy", "v3"), required=True)
    arguments = parser.parse_args(argv)
    try:
        config = load_config(os.environ)
        count = asyncio.run(reset(config, cast(Generation, arguments.generation)))
    except KeyboardInterrupt:
        print("FSM reset interrupted; some conversation keys may already be removed.", file=sys.stderr)
        return 130
    except Exception:
        print(
            "FSM reset failed; some conversation keys may already be removed. Check Redis configuration and connectivity.", file=sys.stderr
        )
        return 1
    print(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

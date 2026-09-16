import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from msu_hub_bot import fsm_reset


def test_reset_config_requires_only_redis_and_preserves_envelope_precedence():
    environment = {
        "HUB_CONFIG_JSON": json.dumps(
            {
                "HUB_NAME": "synthetic",
                "HUB_REDIS_HOST": "redis.invalid",
                "HUB_REDIS_PORT": "6380",
                "HUB_REDIS_DB": "3",
                "HUB_REDIS_PASSWORD": "synthetic-secret",
                "HUB_WIT_TOKENS": "not valid provider JSON",
                "HUB_EDGEDB_DSN": "",
                "HUB_BOT_TOKEN": "",
                "LOGFIRE_TOKEN": "ignored-write-canary",
            }
        ),
        "HUB_REDIS_PORT": "6381",
    }
    before = dict(environment)
    config = fsm_reset.load_config(environment)
    assert (config.namespace, config.host, config.port, config.database) == ("synthetic", "redis.invalid", 6381, 3)
    assert config.password == "synthetic-secret"
    assert environment == before
    assert "synthetic-secret" not in repr(config)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"HUB_REDIS_HOST": ""},
        {"HUB_REDIS_HOST": "redis.invalid", "HUB_REDIS_PORT": "0"},
        {"HUB_REDIS_HOST": "redis.invalid", "HUB_REDIS_PORT": "65536"},
        {"HUB_REDIS_HOST": "redis.invalid", "HUB_REDIS_DB": "-1"},
        {"HUB_REDIS_HOST": "redis.invalid", "HUB_REDIS_DB": "invalid"},
        {"HUB_REDIS_HOST": "redis.invalid", "HUB_NAME": ""},
        {"HUB_REDIS_HOST": "redis.invalid", "HUB_NAME": "hub*"},
        {"HUB_REDIS_HOST": "redis.invalid", "HUB_NAME": "hub?other"},
        {"HUB_REDIS_HOST": "redis.invalid", "HUB_NAME": "hub[12]"},
        {"HUB_REDIS_HOST": "redis.invalid", "HUB_NAME": "hub\\"},
        {"HUB_REDIS_HOST": "redis.invalid", "HUB_NAME": "hub\nother"},
        {"HUB_CONFIG_JSON": "not json"},
        {"HUB_CONFIG_JSON": "[]"},
        {"HUB_CONFIG_JSON": json.dumps({"HUB_REDIS_PORT": 6380})},
        {"HUB_CONFIG_JSON": json.dumps({"HUB_CONFIG_JSON": "{}"})},
    ],
)
def test_reset_config_rejects_missing_or_ambiguous_values(values):
    with pytest.raises((ValueError, TypeError)):
        fsm_reset.load_config(values)


@pytest.mark.parametrize("generation", ["legacy", "v3"])
async def test_reset_selects_one_generation_and_closes_its_decoded_client(monkeypatch, generation):
    client = SimpleNamespace(aclose=AsyncMock())
    factory = Mock(return_value=client)
    legacy, v3 = AsyncMock(return_value=7), AsyncMock(return_value=11)
    monkeypatch.setattr(fsm_reset, "Redis", factory)
    monkeypatch.setattr(fsm_reset, "reset_legacy_fsm", legacy)
    monkeypatch.setattr(fsm_reset, "reset_v3_fsm", v3)
    config = fsm_reset.ResetConfig("synthetic", "redis.invalid", password="synthetic-secret")
    assert await fsm_reset.reset(config, generation) == (7 if generation == "legacy" else 11)
    selected, unused = (legacy, v3) if generation == "legacy" else (v3, legacy)
    selected.assert_awaited_once_with(client, prefix="synthetic")
    unused.assert_not_awaited()
    assert factory.call_args.kwargs == dict(
        host="redis.invalid",
        port=6379,
        password="synthetic-secret",
        db=0,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=10,
    )
    client.aclose.assert_awaited_once()


@pytest.mark.parametrize("failure", [RuntimeError("synthetic"), asyncio.CancelledError()])
async def test_reset_always_closes_client_after_failure_or_cancellation(monkeypatch, failure):
    client = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(fsm_reset, "Redis", Mock(return_value=client))
    monkeypatch.setattr(fsm_reset, "reset_legacy_fsm", AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await fsm_reset.reset(fsm_reset.ResetConfig("synthetic", "redis.invalid"), "legacy")
    client.aclose.assert_awaited_once()


def test_cli_prints_only_count(monkeypatch, capsys):
    monkeypatch.setattr(fsm_reset, "load_config", lambda environment: fsm_reset.ResetConfig("synthetic", "redis.invalid"))
    operation = AsyncMock(return_value=12)
    monkeypatch.setattr(fsm_reset, "reset", operation)
    assert fsm_reset.main(["--generation", "v3"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "12\n" and captured.err == ""
    assert operation.call_args.args[1] == "v3"


@pytest.mark.parametrize("stage", ["configuration", "operation"])
def test_cli_failure_does_not_expose_private_error_or_configuration(monkeypatch, capsys, stage):
    canary = "synthetic-private-error"
    config = fsm_reset.ResetConfig("synthetic", "redis.invalid", password=canary)
    monkeypatch.setattr(
        fsm_reset, "load_config", Mock(return_value=config, side_effect=ValueError(canary) if stage == "configuration" else None)
    )
    operation = AsyncMock(side_effect=RuntimeError(canary))
    monkeypatch.setattr(fsm_reset, "reset", operation)
    assert fsm_reset.main(["--generation", "legacy"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "FSM reset failed" in captured.err and canary not in captured.err
    if stage == "configuration":
        operation.assert_not_awaited()


@pytest.mark.parametrize("arguments", [[], ["--generation", "synthetic-private-value"], ["--unknown", "synthetic-private-value"]])
def test_cli_rejects_invalid_arguments_before_loading_configuration(monkeypatch, capsys, arguments):
    load = Mock()
    monkeypatch.setattr(fsm_reset, "load_config", load)
    with pytest.raises(SystemExit) as caught:
        fsm_reset.main(arguments)
    assert caught.value.code == 2
    load.assert_not_called()
    output = capsys.readouterr()
    assert output.out == "" and "synthetic-private-value" not in output.err


def test_import_and_help_do_not_load_application_settings_or_allocate_services(tmp_path):
    import os
    import subprocess
    import sys

    environment = {key: value for key, value in os.environ.items() if not key.startswith("HUB_")}
    environment["HUB_WIT_TOKENS"] = "not valid provider JSON"
    source = """
import sys
from redis.asyncio import Redis

def forbidden(*args, **kwargs):
    raise AssertionError("Maintenance import allocated a Redis client")

Redis.__init__ = forbidden
from msu_hub_bot import fsm_reset
assert "hub_bot.app" not in sys.modules
assert "msu_hub_bot.settings" not in sys.modules
assert "edgedb" not in sys.modules
try:
    fsm_reset.main(["--help"])
except SystemExit as result:
    assert result.code == 0
else:
    raise AssertionError("Expected help exit")
"""
    result = subprocess.run([sys.executable, "-I", "-c", source], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert "--generation" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("key", ["LOGFIRE_API_KEY", "LOGFIRE_READ_TOKEN", "OTEL_EXPORTER_OTLP_HEADERS"])
def test_reset_envelope_rejects_management_and_arbitrary_telemetry_credentials(key):
    with pytest.raises(ValueError, match="Invalid deployment configuration"):
        fsm_reset.load_config({"HUB_CONFIG_JSON": json.dumps({"HUB_REDIS_HOST": "redis.invalid", key: "private-canary"})})

"""A successful poll proves readiness even when no messages arrive."""

import os
import time
from pathlib import Path


def heartbeat_path() -> Path:
    return Path(os.environ.get("HUB_POLL_HEARTBEAT", "/tmp/msu-hub-bot.poll"))


def mark_poll_success() -> None:
    heartbeat_path().write_text(str(time.monotonic()))


def ready(max_age: float = 180) -> bool:
    try:
        age = time.monotonic() - float(heartbeat_path().read_text())
        return 0 <= age <= max_age
    except OSError, ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(0 if ready() else 1)

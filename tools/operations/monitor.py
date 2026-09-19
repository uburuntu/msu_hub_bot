"""Check maintenance receipts and disk reserve; optionally notify the bot owner."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

MAX_RECEIPT_BYTES = 64 * 1024
ALERTS = {
    "backup": "Нет свежего успешного резервного копирования базы.",
    "retention": "Нет свежего успешного запуска очистки по срокам хранения.",
    "restore": "Проверка восстановления отсутствует или устарела.",
    "disk": "Свободное место ниже резерва для резервного копирования.",
}
NOTIFY = """
import asyncio,json,sys
from aiogram import Bot
from msu_hub_bot.settings import Settings
async def main():
    config=Settings()
    if config.owner_id <= 0: raise ValueError("Owner is not configured")
    message=json.load(sys.stdin)["text"]
    async with Bot(config.bot_token) as bot:
        async with asyncio.timeout(20):
            await bot.send_message(config.owner_id,message,parse_mode=None,disable_notification=True)
try: asyncio.run(main())
except Exception: raise SystemExit("Operational notification failed") from None
"""


def receipt(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > MAX_RECEIPT_BYTES:
        raise ValueError("Invalid receipt file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid receipt object")
    return value


def fresh(value: dict, key: str, maximum_age: float, now: datetime) -> bool:
    stamp = datetime.fromisoformat(value[key])
    return stamp.tzinfo is not None and -300 <= (now - stamp).total_seconds() <= maximum_age


def evaluate(evidence: Path, restore: Path, disk: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    checks = {}
    for name, filename, seconds in (("backup", "backup-latest.json", 36 * 3600), ("retention", "retention-latest.json", 3600)):
        try:
            value = receipt(evidence / filename)
            checks[name] = value.get("complete") is True and fresh(value, "completed_at", seconds, now)
            if name == "backup":
                checks[name] = checks[name] and value.get("local_encrypted_backup_complete") is True
            else:
                checks[name] = (
                    checks[name] and value.get("bounded_run_complete") is True and value.get("durable_entities_unchanged") is True
                )
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            checks[name] = False
    try:
        value = receipt(restore)
        checks["restore"] = value.get("restored_verified") is True and fresh(value, "verified_at", 30 * 86400, now)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        checks["restore"] = False
    try:
        usage = shutil.disk_usage(disk)
        checks["disk"] = usage.free >= 40 * 2**30 and usage.free >= usage.total * 0.1
    except OSError:
        checks["disk"] = False
    return {
        "checked_at": now.isoformat(),
        "ok": all(checks.values()),
        "checks": checks,
        "issues": [name for name, good in checks.items() if not good],
    }


def message_for(issues: list[str]) -> str:
    if not issues:
        return "✅ Инфраструктура бота: проверки снова проходят."
    return "🔧 Инфраструктура бота требует внимания:\n\n" + "\n".join("• " + ALERTS[name] for name in issues)


def notify(report: dict, state_path: Path, *, container: str, now: float | None = None) -> bool:
    """One message per change or six-hour reminder; failed sends never acknowledge."""
    now = time.time() if now is None else now
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = receipt(state_path)
        except (OSError, ValueError):
            state = {}
        issues = report["issues"]
        previous = state.get("issues", [])
        last_sent = state.get("sent_at", 0)
        changed = previous != issues
        due = bool(issues) and (not isinstance(last_sent, (float, int)) or now - last_sent >= 6 * 3600)
        if not changed and not due:
            return False
        result = subprocess.run(
            ["docker", "exec", "-i", container, "/opt/msu_hub_bot/.venv/bin/python", "-c", NOTIFY],
            input=json.dumps({"text": message_for(issues)}),
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError("Operational notification failed")
        fd, temporary = tempfile.mkstemp(prefix=".monitor-", dir=state_path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"issues": issues, "sent_at": now}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, state_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--restore-receipt", type=Path, required=True)
    parser.add_argument("--disk", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--container", default="msu_hub_bot")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    if args.notify and args.state is None:
        parser.error("--notify requires --state")
    os.umask(0o077)
    report = evaluate(args.evidence, args.restore_receipt, args.disk)
    if args.notify:
        report["notification_sent"] = notify(report, args.state, container=args.container)
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError):
        raise SystemExit("Operational monitor failed; inspect its configured paths and notification transport") from None

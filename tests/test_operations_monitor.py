"""Operational alerts use evidence, coalesce repeats and never leak receipt bodies."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.operations import monitor


def fixture(tmp_path, monkeypatch):
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    good = {
        "complete": True,
        "completed_at": now.isoformat(),
        "local_encrypted_backup_complete": True,
        "bounded_run_complete": True,
        "durable_entities_unchanged": True,
        "private": "DO_NOT_EXPORT",
    }
    for name in ("backup", "retention"):
        (tmp_path / f"{name}-latest.json").write_text(json.dumps(good))
    restore = tmp_path / "restore.json"
    restore.write_text(json.dumps({"restored_verified": True, "verified_at": now.isoformat()}))
    monkeypatch.setattr(monitor.shutil, "disk_usage", lambda path: SimpleNamespace(free=100 * 2**30, total=500 * 2**30))
    return now, restore


def test_freshness_and_real_success_are_required_without_offhost_policy(tmp_path, monkeypatch):
    now, restore = fixture(tmp_path, monkeypatch)
    result = monitor.evaluate(tmp_path, restore, tmp_path, now=now)
    assert result["ok"] and result["issues"] == []
    assert "DO_NOT_EXPORT" not in json.dumps(result)
    result = monitor.evaluate(tmp_path, restore, tmp_path, now=now + timedelta(hours=37))
    assert result["issues"] == ["backup", "retention"]
    result = monitor.evaluate(tmp_path, restore, tmp_path, now=now + timedelta(days=31))
    assert result["issues"] == ["backup", "retention", "restore"]


def test_invalid_or_failed_receipts_are_not_fresh_success(tmp_path, monkeypatch):
    now, restore = fixture(tmp_path, monkeypatch)
    (tmp_path / "backup-latest.json").write_text('{"complete":false}')
    (tmp_path / "retention-latest.json").write_text("invalid")
    restore.write_text("[]")
    monkeypatch.setattr(monitor.shutil, "disk_usage", lambda path: SimpleNamespace(free=1, total=100))
    assert monitor.evaluate(tmp_path, restore, tmp_path, now=now)["issues"] == list(monitor.ALERTS)


def test_notifications_change_repeat_recover_and_preserve_failed_send(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    send = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(monitor.subprocess, "run", send)
    assert not monitor.notify({"issues": []}, state, container="fixture", now=100000)
    assert not monitor.notify({"issues": ["disk"]}, state, container="fixture", now=100000)
    assert monitor.notify({"issues": ["disk"]}, state, container="fixture", now=100600)
    assert not monitor.notify({"issues": ["disk"]}, state, container="fixture", now=100601)
    assert monitor.notify({"issues": ["disk"]}, state, container="fixture", now=130000)
    saved = state.read_text()
    send.return_value.returncode = 1
    with pytest.raises(RuntimeError):
        monitor.notify({"issues": []}, state, container="fixture", now=130001)
    assert state.read_text() == saved
    send.return_value.returncode = 0
    assert monitor.notify({"issues": []}, state, container="fixture", now=130002)
    assert state.stat().st_mode & 0o077 == 0
    assert send.call_count == 4


def test_brief_failures_inside_the_grace_period_stay_silent(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    send = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(monitor.subprocess, "run", send)
    for now, issues in ((0, ["retention"]), (300, []), (600, ["retention"]), (900, ["retention"]), (1199, ["retention"])):
        assert not monitor.notify({"issues": issues}, state, container="fixture", now=now)
    send.assert_not_called()
    assert monitor.notify({"issues": ["retention"]}, state, container="fixture", now=1200)
    assert "срокам хранения" in json.loads(send.call_args.kwargs["input"])["text"]
    # A new failure waits for its own grace period while the alerted one stays reported.
    assert not monitor.notify({"issues": ["retention", "disk"]}, state, container="fixture", now=1500)
    assert monitor.notify({"issues": ["retention", "disk"]}, state, container="fixture", now=2100)
    assert monitor.notify({"issues": []}, state, container="fixture", now=2400)
    assert "снова проходят" in json.loads(send.call_args.kwargs["input"])["text"]


def test_alerts_from_the_previous_state_format_stay_acknowledged(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"issues": ["backup"], "sent_at": 0}))
    send = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(monitor.subprocess, "run", send)
    assert not monitor.notify({"issues": ["backup"]}, state, container="fixture", now=3600)
    send.assert_not_called()
    assert json.loads(state.read_text()) == {"issues": ["backup"], "sent_at": 0, "since": {"backup": 3600}}

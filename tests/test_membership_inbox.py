"""The polling journal keeps authoritative facts across interruption and retry."""

import asyncio
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update

from msu_hub_bot.storage.errors import RepositoryFailure, RepositoryUnavailable
from msu_hub_bot.storage.models import ChatObservation, MembershipBatch, MembershipObservation, UserObservation
from msu_hub_bot.storage.observations import membership_batch
from msu_hub_bot.storage.supabase import SupabaseRepository
from msu_hub_bot.telegram.membership_inbox import MembershipInbox, MembershipInboxError

NOW = datetime(2026, 9, 20, tzinfo=UTC)
USER = {"id": 42, "is_bot": False, "first_name": "Synthetic"}
CHAT = {"id": -1001, "type": "supergroup", "title": "Synthetic"}


def membership_update(identifier=100):
    return Update.model_validate(
        {
            "update_id": identifier,
            "chat_member": {
                "chat": CHAT,
                "from": {**USER, "id": 77},
                "date": NOW,
                "old_chat_member": {"status": "left", "user": USER},
                "new_chat_member": {"status": "member", "user": USER},
            },
        }
    )


def batch(identifier=100):
    result = membership_batch(membership_update(identifier), received_at=NOW)
    assert result is not None
    return result


async def wait_empty(inbox):
    async with asyncio.timeout(2):
        while await inbox.pending():
            await asyncio.sleep(0.001)


async def finish(inbox, worker=None):
    inbox.stop()
    try:
        if worker is not None:
            await asyncio.wait_for(worker, timeout=1)
    finally:
        await inbox.close()


async def test_capture_saves_only_direct_membership_facts_in_private_storage(tmp_path):
    path = tmp_path / "private" / "inbox.sqlite3"
    inbox = MembershipInbox(path, 999, AsyncMock())
    await inbox.open()
    update = Update.model_validate(
        {
            "update_id": 101,
            "message": {
                "message_id": 12,
                "date": NOW,
                "chat": {**CHAT, "private_extra": "CHAT_PROFILE_CANARY"},
                "from": {**USER, "id": 77},
                "new_chat_members": [{**USER, "private_extra": "USER_PROFILE_CANARY"}],
                "text": "MESSAGE_BODY_CANARY",
                "reply_to_message": {
                    "message_id": 11,
                    "date": NOW,
                    "chat": CHAT,
                    "from": {**USER, "id": 88},
                    "text": "REPLY_BODY_CANARY",
                },
            },
        }
    )
    ordinary = Update.model_validate({"update_id": 102, "message": update.message.model_copy(update={"new_chat_members": None})})
    await inbox.capture([update, ordinary])
    assert await inbox.pending() == 1
    await inbox.close()
    with sqlite3.connect(path) as connection:
        payload = connection.execute("SELECT payload FROM pending").fetchone()[0]
    data = json.loads(payload)
    assert set(data) == {"update_id", "received_at", "users", "chats", "memberships"}
    assert [user["user_id"] for user in data["users"]] == [42]
    assert [member["user_id"] for member in data["memberships"]] == [42]
    assert all("profile" not in record for record in data["users"] + data["chats"])
    assert "CANARY" not in payload and b"CANARY" not in path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


async def test_redelivery_deduplicates_receipt_time_and_strips_caller_profiles(tmp_path):
    inbox = MembershipInbox(tmp_path / "inbox.sqlite3", 999, AsyncMock(), max_rows=1)
    await inbox.open()
    first = batch()
    first.users[0].profile = {"body": "USER_PROFILE_CANARY"}
    first.chats[0].profile = {"body": "CHAT_PROFILE_CANARY"}
    await inbox.enqueue([first])
    await inbox.enqueue([first.model_copy(update={"received_at": NOW + timedelta(hours=1)})])
    assert await inbox.pending() == 1
    await inbox.close()
    assert b"CANARY" not in inbox.path.read_bytes()


@pytest.mark.parametrize("explicit_clear", [False, True])
async def test_sparse_identity_fields_survive_persist_reopen_and_rpc_replay(tmp_path, explicit_clear):
    original = batch()
    # Extraction asserts absent names as cleared; build sparse sightings to test presence storage.
    user, chat = original.users[0], original.chats[0]
    original.users[0] = type(user).model_validate(user.model_dump(exclude_unset=True, exclude={"username", "last_name"}))
    original.chats[0] = type(chat).model_validate(chat.model_dump(exclude_unset=True, exclude={"username", "first_name", "last_name"}))
    if explicit_clear:
        original.users[0].username = None
        original.chats[0].username = None
    path = tmp_path / "inbox.sqlite3"
    inbox = MembershipInbox(path, 999, AsyncMock())
    await inbox.open()
    await inbox.enqueue([original])
    await inbox.close()
    with sqlite3.connect(path) as connection:
        saved = MembershipBatch.model_validate_json(connection.execute("SELECT payload FROM pending").fetchone()[0])
    assert ("username" in saved.users[0].model_fields_set) is explicit_clear
    assert ("username" in saved.chats[0].model_fields_set) is explicit_clear
    assert "last_name" not in saved.users[0].model_fields_set
    assert "language_code" not in saved.users[0].model_fields_set
    assert "first_name" not in saved.chats[0].model_fields_set
    assert "last_name" not in saved.chats[0].model_fields_set
    known_user = {"username": "known_user", "last_name": "Known", "language_code": "ru"}
    known_chat = {"username": "known_chat", "first_name": "Known", "last_name": "Chat"}

    async def apply_wire(rpc, request, *args, **kwargs):
        assert rpc == "observe_memberships"
        payload = request["p_observation"]
        user, chat = payload["users"][0], payload["chats"][0]
        assert ("username" in user) is explicit_clear
        assert ("username" in chat) is explicit_clear
        # Apply the repository's actual sparse RPC payload to existing metadata.
        known_user.update({name: value for name, value in user.items() if name in known_user})
        known_chat.update({name: value for name, value in chat.items() if name in known_chat})

    class WireRepository:
        observe_memberships = SupabaseRepository.observe_memberships
        _rpc = AsyncMock(side_effect=apply_wire)

    repository = WireRepository()
    inbox = MembershipInbox(path, 999, repository)
    await inbox.open()
    worker = asyncio.create_task(inbox.run())
    try:
        await wait_empty(inbox)
    finally:
        await finish(inbox, worker)
    repository._rpc.assert_awaited_once()
    assert known_user == {"username": None if explicit_clear else "known_user", "last_name": "Known", "language_code": "ru"}
    assert known_chat == {"username": None if explicit_clear else "known_chat", "first_name": "Known", "last_name": "Chat"}


async def test_replay_preserves_default_observation_and_receipt_clocks(tmp_path):
    original = MembershipBatch(
        update_id=123,
        users=[UserObservation(user_id=42, is_bot=False, first_name="Synthetic")],
        chats=[ChatObservation(chat_id=-1001, type="supergroup")],
        memberships=[MembershipObservation(chat_id=-1001, user_id=42)],
    )
    path = tmp_path / "inbox.sqlite3"
    inbox = MembershipInbox(path, 999, AsyncMock())
    await inbox.open()
    await inbox.enqueue([original])
    await inbox.close()
    repository = AsyncMock()
    inbox = MembershipInbox(path, 999, repository)
    await inbox.open()
    worker = asyncio.create_task(inbox.run())
    await wait_empty(inbox)
    await finish(inbox, worker)
    replayed = repository.observe_memberships.await_args.args[0]
    assert replayed.received_at == original.received_at
    assert replayed.users[0].observed_at == original.users[0].observed_at
    assert replayed.chats[0].observed_at == original.chats[0].observed_at
    assert replayed.memberships[0].observed_at == original.memberships[0].observed_at
    assert "status" not in replayed.memberships[0].model_fields_set


@pytest.mark.parametrize("limit", ["rows", "bytes"])
async def test_capacity_failure_rolls_back_the_whole_polling_batch(tmp_path, limit):
    first = batch()
    payload = first.model_dump(
        mode="json", exclude_unset=True, exclude={"users": {"__all__": {"profile"}}, "chats": {"__all__": {"profile"}}}
    )
    size = len(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    limits = {"max_rows": 2} if limit == "rows" else {"max_bytes": size * 2 + 5}
    inbox = MembershipInbox(tmp_path / "inbox.sqlite3", 999, AsyncMock(), **limits)
    await inbox.open()
    await inbox.enqueue([first])
    with pytest.raises(MembershipInboxError, match="full"):
        await inbox.enqueue([batch(101), batch(102)])
    assert await inbox.pending() == 1
    await inbox.enqueue([first])
    assert await inbox.pending() == 1
    await inbox.close()


async def test_pending_facts_survive_process_exit_without_connection_close(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    payload = batch().model_dump_json()
    program = """
import asyncio, os, sys
from pathlib import Path
from unittest.mock import AsyncMock
from msu_hub_bot.storage.models import MembershipBatch
from msu_hub_bot.telegram.membership_inbox import MembershipInbox
async def main():
    inbox = MembershipInbox(Path(sys.argv[1]), 999, AsyncMock())
    await inbox.open()
    await inbox.enqueue([MembershipBatch.model_validate_json(sys.argv[2])])
    os._exit(0)
asyncio.run(main())
"""
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", program, str(path), payload], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    repository = AsyncMock()
    inbox = MembershipInbox(path, 999, repository)
    await inbox.open()
    assert await inbox.pending() == 1
    worker = asyncio.create_task(inbox.run())
    await wait_empty(inbox)
    await finish(inbox, worker)
    repository.observe_memberships.assert_awaited_once_with(batch())


async def test_remote_failures_retry_then_ack_only_success_and_log_once(tmp_path, caplog):
    started, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def deliver(value):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        started.set()
        await release.wait()

    repository = AsyncMock()
    repository.observe_memberships.side_effect = deliver
    inbox = MembershipInbox(tmp_path / "inbox.sqlite3", 999, repository, retry_seconds=0.001)
    await inbox.open()
    await inbox.enqueue([batch()])
    worker = asyncio.create_task(inbox.run())
    await asyncio.wait_for(started.wait(), timeout=2)
    assert await inbox.pending() == 1
    release.set()
    await wait_empty(inbox)
    await finish(inbox, worker)
    assert attempts == 4
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0].message == "Membership ingestion delayed; saved events will be retried"


async def test_cancelled_remote_delivery_is_replayed_after_reopen(tmp_path):
    entered = asyncio.Event()

    async def deliver(value):
        entered.set()
        await asyncio.Future()

    repository = AsyncMock()
    repository.observe_memberships.side_effect = deliver
    path = tmp_path / "inbox.sqlite3"
    inbox = MembershipInbox(path, 999, repository)
    await inbox.open()
    await inbox.enqueue([batch()])
    worker = asyncio.create_task(inbox.run())
    await asyncio.wait_for(entered.wait(), timeout=1)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert await inbox.pending() == 1
    await inbox.close()
    replayed = AsyncMock()
    inbox = MembershipInbox(path, 999, replayed)
    await inbox.open()
    worker = asyncio.create_task(inbox.run())
    await wait_empty(inbox)
    await finish(inbox, worker)
    replayed.observe_memberships.assert_awaited_once_with(batch())


async def test_cancelled_sqlite_wait_cannot_close_underneath_running_commit(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    path = tmp_path / "inbox.sqlite3"
    inbox = MembershipInbox(path, 999, AsyncMock())
    await inbox.open()
    persist = inbox._persist

    def blocked(records):
        entered.set()
        assert release.wait(2)
        persist(records)

    monkeypatch.setattr(inbox, "_persist", blocked)
    writing = asyncio.create_task(inbox.enqueue([batch()]))
    assert await asyncio.to_thread(entered.wait, 1)
    writing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writing
    closing = asyncio.create_task(inbox.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, timeout=2)
    inbox = MembershipInbox(path, 999, AsyncMock())
    await inbox.open()
    assert await inbox.pending() == 1
    await inbox.close()


async def test_cancelled_close_joins_cleanup_after_queued_sqlite_work(tmp_path):
    entered, release = threading.Event(), threading.Event()
    inbox = MembershipInbox(tmp_path / "inbox.sqlite3", 999, AsyncMock())
    await inbox.open()

    def blocked():
        entered.set()
        assert release.wait(2)

    writing = asyncio.create_task(inbox._io(blocked))
    assert await asyncio.to_thread(entered.wait, 1)
    closing = asyncio.create_task(inbox.close())
    other_closing = asyncio.create_task(inbox.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not other_closing.done()
    release.set()
    await asyncio.wait_for(writing, timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, timeout=1)
    await asyncio.wait_for(other_closing, timeout=1)
    assert inbox._connection is None and inbox._closed
    assert inbox._executor._shutdown
    await inbox.close()


async def test_delivery_timeout_keeps_row_and_stop_interrupts_retry_wait(tmp_path):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def deliver(value):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    repository = AsyncMock()
    repository.observe_memberships.side_effect = deliver
    inbox = MembershipInbox(tmp_path / "inbox.sqlite3", 999, repository, delivery_timeout=0.01, retry_seconds=30)
    await inbox.open()
    await inbox.enqueue([batch()])
    worker = asyncio.create_task(inbox.run())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert await inbox.pending() == 1
    await finish(inbox, worker)
    repository.observe_memberships.assert_awaited_once()


async def test_stop_wakes_an_empty_consumer_and_close_is_idempotent(tmp_path):
    inbox = MembershipInbox(tmp_path / "inbox.sqlite3", 999, AsyncMock())
    await inbox.open()
    worker = asyncio.create_task(inbox.run())
    await asyncio.sleep(0)
    await finish(inbox, worker)
    await inbox.close()
    with pytest.raises(MembershipInboxError, match="not accepting"):
        await inbox.enqueue([batch()])


async def test_another_bot_cannot_deliver_saved_memberships(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    inbox = MembershipInbox(path, 999, AsyncMock())
    await inbox.open()
    await inbox.enqueue([batch()])
    await inbox.close()
    other = MembershipInbox(path, 1000, AsyncMock())
    try:
        with pytest.raises(MembershipInboxError, match="another bot"):
            await other.open()
    finally:
        await other.close()
    original = MembershipInbox(path, 999, AsyncMock())
    await original.open()
    assert await original.pending() == 1
    await original.close()


@pytest.mark.parametrize("unsafe", ["directory", "file", "symlink", "hardlink", "corrupt", "version"])
async def test_unsafe_or_incompatible_journal_fails_closed_without_deleting_it(tmp_path, unsafe):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "inbox.sqlite3"
    if unsafe == "directory":
        private.chmod(0o755)
    elif unsafe in {"file", "hardlink", "corrupt", "version"}:
        path.touch(mode=0o600)
        if unsafe == "file":
            path.chmod(0o644)
        elif unsafe == "hardlink":
            os.link(path, private / "second.sqlite3")
        elif unsafe == "corrupt":
            path.write_text("PRIVATE_CORRUPTION_CANARY")
        else:
            with sqlite3.connect(path) as connection:
                connection.execute("PRAGMA user_version=99")
    else:
        target = private / "target.sqlite3"
        target.touch(mode=0o600)
        path.symlink_to(target)
    inbox = MembershipInbox(path, 999, AsyncMock())
    try:
        with pytest.raises(MembershipInboxError) as failure:
            await inbox.open()
        assert str(path) not in str(failure.value)
        assert "CANARY" not in str(failure.value)
        if unsafe != "directory":
            assert path.exists()
    finally:
        await inbox.close()


async def test_oversize_capture_fails_without_partially_staging(tmp_path):
    inbox = MembershipInbox(tmp_path / "inbox.sqlite3", 999, AsyncMock())
    await inbox.open()
    with pytest.raises(MembershipInboxError, match="too large"):
        await inbox.capture([membership_update()] * 101)
    oversized = batch()
    oversized.users[0].first_name = "a" * (1024 * 1024)
    with pytest.raises(MembershipInboxError, match="too large"):
        await inbox.enqueue([batch(), oversized])
    assert await inbox.pending() == 0
    await inbox.close()


async def test_corrupt_saved_payload_is_retained_and_diagnostic_is_sanitized(tmp_path, caplog):
    path = tmp_path / "inbox.sqlite3"
    inbox = MembershipInbox(path, 999, AsyncMock(), retry_seconds=0.001)
    await inbox.open()
    await inbox.enqueue([batch()])
    await inbox.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE pending SET payload=?", ('{"private":"PAYLOAD_CANARY"}',))
    repository = AsyncMock()
    inbox = MembershipInbox(path, 999, repository, retry_seconds=0.001)
    await inbox.open()
    worker = asyncio.create_task(inbox.run())
    async with asyncio.timeout(1):
        while not caplog.records:
            await asyncio.sleep(0.001)
    assert await inbox.pending() == 1
    await finish(inbox, worker)
    repository.observe_memberships.assert_not_awaited()
    assert "PAYLOAD_CANARY" not in caplog.text


async def test_failed_period_emits_one_safe_incident_and_one_recovery(tmp_path, monkeypatch):
    from msu_hub_bot.telemetry import Telemetry
    from telemetry_helpers import Capture, config

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    await telemetry.start()
    repository = AsyncMock()
    repository.observe_memberships.side_effect = [
        RepositoryUnavailable(RepositoryFailure.UNAVAILABLE),
        RepositoryUnavailable(RepositoryFailure.UNAVAILABLE),
        None,
    ]
    inbox = MembershipInbox(tmp_path / "inbox.sqlite3", 999, repository, retry_seconds=0.001, telemetry=telemetry)
    await inbox.open()
    await inbox.enqueue([batch()])
    worker = asyncio.create_task(inbox.run())
    try:
        await wait_empty(inbox)
    finally:
        await finish(inbox, worker)
        await telemetry.close()
    attributes = [{attribute.key: attribute.value.string_value for attribute in span.attributes} for span in capture.spans()]
    spans = [values for values in attributes if values.get("operation") == "membership.ingest"]
    assert len(spans) == 2
    assert {values["outcome"] for values in spans} == {"unavailable", "success"}
    text = capture.serialized()
    assert str(tmp_path) not in text and "Synthetic" not in text
    assert '"user_id"' not in text and '"chat_id"' not in text

"""Reminder scheduling, ownership and uncertain-send recovery without network."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery

from msu_hub_bot.commands.remind import Remind, ReminderCallback, keyboard
from msu_hub_bot.reminders import ReminderError, ReminderService, Schedule, parse_schedule
from msu_hub_bot.reminders.service import MAX_DELIVERY_ATTEMPTS, TERMINAL_RETENTION
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker
from msu_hub_bot.storage.supabase import RepositoryFailure, RepositoryUnavailable
from msu_hub_bot.telegram.filters import MetaInfo
from quiz_helpers import FeatureFixture
from telegram_helpers import make_bot, make_message

NOW = datetime(2030, 1, 1, 10, tzinfo=UTC)


@pytest.fixture
async def rig():
    backend = FeatureFixture()
    backend.now = NOW
    bot = make_bot()
    store = FeatureStore(backend)
    worker = FeatureWorker(store)
    service = ReminderService(bot, store, worker)
    service.clock = lambda: backend.now
    yield SimpleNamespace(backend=backend, bot=bot, store=store, worker=worker, service=service)
    await bot.session.close()


async def create(rig, *, author_id=42, chat_id=-123, thread_id=17, source_message_id=1, text="Tea <&>", due=None):
    return await rig.service.create(
        author_id=author_id,
        author_name="User <name>",
        chat_id=chat_id,
        thread_id=thread_id,
        source_message_id=source_message_id,
        schedule=Schedule(due_at=due or NOW + timedelta(hours=1), text=text),
    )


async def drain(rig):
    for _ in range(10):
        if not await rig.worker.run_once():
            return
    raise AssertionError("Reminder worker did not become idle")


def restart(rig):
    rig.worker = FeatureWorker(rig.store)
    rig.service = ReminderService(rig.bot, rig.store, rig.worker)
    rig.service.clock = lambda: rig.backend.now


@pytest.mark.parametrize(
    "text,seconds,body",
    [
        ("in 15m tea", 900, "tea"),
        ("через 2ч 30м размяться", 9000, "размяться"),
        ("in 1 week 2 days | book", 777600, "book"),
        ("через 30 секунд чай", 30, "чай"),
        ("in 2h30m", 9000, ""),
        ("in 3d text\nsecond line", 259200, "text\nsecond line"),
    ],
)
def test_relative_parser(text, seconds, body):
    schedule = parse_schedule(text, NOW)
    assert schedule.due_at == NOW + timedelta(seconds=seconds)
    assert schedule.text == body and schedule.timezone == "Europe/Moscow"


def test_explicit_dates_preserve_timezone_and_support_years_ahead():
    value = parse_schedule("at 2040-04-15 09:30 [Europe/London] встреча", NOW)
    assert value.due_at == datetime(2040, 4, 15, 8, 30, tzinfo=UTC)
    assert value.timezone == "Europe/London" and value.text == "встреча"
    value = parse_schedule("2030-01-02 09:00 чай", NOW)
    assert value.due_at == datetime(2030, 1, 2, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "in 0m tea",
        "in -1h tea",
        "tomorrow tea",
        "at 2029-01-01 12:00 tea",
        "at 2030-02-31 12:00 tea",
        "at 2030-03-31 01:30 [Europe/London] tea",
        "at 2030-10-27 01:30 [Europe/London] tea",
        "at 2030-01-02 12:00 [Wrong/Zone] tea",
        "in 999999999w tea",
        "in 1m " + "😀" * 1501,
    ],
)
def test_invalid_ambiguous_or_oversize_schedule_is_rejected(text):
    with pytest.raises(ReminderError):
        parse_schedule(text, NOW)


async def test_create_is_atomic_durable_and_idempotent_per_source_message(rig):
    first, duplicate = await asyncio.gather(create(rig), create(rig))
    assert first.key == duplicate.key
    assert len(rig.backend.records) == 1 and len(rig.backend.jobs) == 1
    assert first.expires_at is None
    rig.backend.now += timedelta(minutes=10)
    duplicate = await create(rig, due=NOW + timedelta(hours=2))
    assert duplicate.value.due_at == first.value.due_at
    tx = next(request for operation, request in rig.backend.calls if operation == "commit")
    assert len(tx["puts"]) == len(tx["jobs"]) == 1 and tx["guards"][0]["etag"] is None
    restart(rig)
    assert (await rig.service.get(42, first.key)).value == first.value


async def test_owner_and_destination_are_checked_for_every_control(rig):
    record = await create(rig)
    assert await rig.service.list(43) == []
    assert await rig.service.list(42, chat_id=-999, thread_id=17) == []
    assert await rig.service.list(42, chat_id=-123, thread_id=18) == []
    assert len(await rig.service.list(42)) == 1
    for operation in (rig.service.get, rig.service.cancel, rig.service.retry):
        with pytest.raises(ReminderError):
            await operation(43, record.key)
        with pytest.raises(ReminderError):
            await operation(42, record.key, chat_id=-123, thread_id=18)
    with pytest.raises(ReminderError):
        await rig.service.reschedule(43, record.key, Schedule(due_at=NOW + timedelta(days=1)))
    assert (await rig.service.get(42, record.key)).value.status == "pending"


async def test_reschedule_and_cancel_fence_old_work_and_stale_buttons(rig):
    record = await create(rig)
    changed = await rig.service.reschedule(42, record.key, Schedule(due_at=NOW + timedelta(days=3000)), expected_etag=record.etag)
    assert changed.value.text == record.value.text and changed.expires_at is None
    with pytest.raises(ReminderError):
        await rig.service.cancel(42, record.key, expected_etag=record.etag)
    cancelled = await rig.service.cancel(42, record.key, expected_etag=changed.etag)
    assert cancelled.value.status == "cancelled"
    rig.backend.now = changed.value.due_at
    await drain(rig)
    assert not rig.bot.session.methods
    assert await rig.service.list(42) == []


async def test_delivery_is_independent_of_old_message_and_marks_lateness(rig):
    record = await create(rig)
    rig.backend.now = record.value.due_at + timedelta(days=3)
    restart(rig)
    await drain(rig)
    delivered = await rig.service.get(42, record.key)
    assert delivered.value.status == "delivered" and delivered.value.delivered_message_id == 1
    (method,) = rig.bot.session.methods
    assert isinstance(method, SendMessage)
    assert method.chat_id == -123 and method.message_thread_id == 17 and method.reply_parameters is None
    assert "опозданием" in method.text and "Tea <&>" in method.text
    assert method.parse_mode is None and method.link_preview_options.is_disabled
    assert method.entities[0].url == "tg://user?id=42"
    await drain(rig)
    assert len(rig.bot.session.methods) == 1


async def test_send_starts_only_after_sending_and_reconciliation_are_committed(rig, monkeypatch):
    record = await create(rig)
    original = rig.bot.session.make_request

    async def inspect(bot, method, timeout=None):
        current = await rig.service.get(42, record.key)
        assert current.value.status == "sending"
        assert any(job["kind"] == "reconcile" and job["state"] == "pending" for job in rig.backend.jobs.values())
        assert timeout == 15
        return await original(bot, method, timeout)

    monkeypatch.setattr(rig.bot.session, "make_request", inspect)
    rig.backend.now = record.value.due_at
    await drain(rig)


async def test_ambiguous_send_holds_until_explicit_owner_retry(rig, monkeypatch):
    record = await create(rig)
    original = rig.bot.session.make_request
    calls = 0

    async def uncertain(bot, method, timeout=None):
        nonlocal calls
        calls += 1
        await original(bot, method, timeout)
        raise TimeoutError("response lost")

    monkeypatch.setattr(rig.bot.session, "make_request", uncertain)
    rig.backend.now = record.value.due_at
    await drain(rig)
    assert (await rig.service.get(42, record.key)).value.status == "uncertain"
    restart(rig)
    await drain(rig)
    assert calls == 1
    monkeypatch.setattr(rig.bot.session, "make_request", original)
    await rig.service.retry(42, record.key)
    await drain(rig)
    assert (await rig.service.get(42, record.key)).value.status == "delivered"
    assert len(rig.bot.session.methods) == 2


async def test_process_cancellation_after_send_marker_is_reconciled_without_send(rig, monkeypatch):
    record = await create(rig)

    async def cancelled(bot, method, timeout=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(rig.bot.session, "make_request", cancelled)
    rig.backend.now = record.value.due_at
    with pytest.raises(asyncio.CancelledError):
        await rig.worker.run_once()
    assert (await rig.service.get(42, record.key)).value.status == "sending"
    rig.backend.now += timedelta(seconds=90)
    restart(rig)
    await drain(rig)
    assert (await rig.service.get(42, record.key)).value.status == "uncertain"
    assert not rig.bot.session.methods


async def test_database_failure_after_telegram_success_never_repeats_delivery(rig, monkeypatch):
    record = await create(rig)
    original = rig.backend.feature_request
    reject_completion = True

    async def failure(operation, request):
        if reject_completion and operation == "commit" and any(row["payload"].get("status") == "delivered" for row in request["puts"]):
            raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        return await original(operation, request)

    monkeypatch.setattr(rig.backend, "feature_request", failure)
    rig.backend.now = record.value.due_at
    await rig.worker.run_once()
    assert (await rig.service.get(42, record.key)).value.status == "sending"
    assert len(rig.bot.session.methods) == 1
    reject_completion = False
    rig.backend.now += timedelta(seconds=90)
    restart(rig)
    await drain(rig)
    assert (await rig.service.get(42, record.key)).value.status == "uncertain"
    assert len(rig.bot.session.methods) == 1


async def test_lost_response_after_delivered_commit_preserves_confirmed_delivery(rig, monkeypatch):
    record = await create(rig)
    original = rig.backend.feature_request
    attempts = []

    async def lost(operation, request):
        result = await original(operation, request)
        if operation == "commit" and any(row["payload"].get("status") == "delivered" for row in request["puts"]):
            attempts.append(request)
            if len(attempts) == 1:
                raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        return result

    monkeypatch.setattr(rig.backend, "feature_request", lost)
    rig.backend.now = record.value.due_at
    await drain(rig)
    assert (await rig.service.get(42, record.key)).value.status == "delivered"
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert len(rig.bot.session.methods) == 1


async def test_rejected_send_is_failed_and_does_not_retry(rig, monkeypatch):
    record = await create(rig)

    async def denied(bot, method, timeout=None):
        raise TelegramBadRequest(method, "chat not found")

    monkeypatch.setattr(rig.bot.session, "make_request", denied)
    rig.backend.now = record.value.due_at
    await drain(rig)
    current = await rig.service.get(42, record.key)
    assert current.value.status == "failed" and current.value.failure == "rejected"
    assert all(job["state"] != "pending" for job in rig.backend.jobs.values() if job["kind"] == "deliver")


async def test_definitive_rate_limit_retries_after_delay_and_has_finite_attempts(rig, monkeypatch):
    record = await create(rig)
    calls = 0

    async def limited(bot, method, timeout=None):
        nonlocal calls
        calls += 1
        raise TelegramRetryAfter(method, "slow down", retry_after=45)

    monkeypatch.setattr(rig.bot.session, "make_request", limited)
    rig.backend.now = record.value.due_at
    for attempt in range(MAX_DELIVERY_ATTEMPTS):
        await drain(rig)
        assert calls == attempt + 1
        rig.backend.now += timedelta(seconds=44)
        await drain(rig)
        assert calls == attempt + 1
        rig.backend.now += timedelta(seconds=1)
    current = await rig.service.get(42, record.key)
    assert current.value.status == "failed" and current.value.failure == "rate_limit"


async def test_terminal_cleanup_deletes_text_and_uncertain_dependencies_after_30_days(rig, monkeypatch):
    record = await create(rig)

    async def uncertain(bot, method, timeout=None):
        raise TimeoutError()

    monkeypatch.setattr(rig.bot.session, "make_request", uncertain)
    rig.backend.now = record.value.due_at
    await drain(rig)
    rig.backend.now += TERMINAL_RETENTION - timedelta(seconds=1)
    await drain(rig)
    assert len(await rig.service.list(42)) == 1
    rig.backend.now += timedelta(seconds=1)
    await drain(rig)
    assert await rig.service.list(42) == []
    assert all(job["state"] in {"cancelled", "complete"} for job in rig.backend.jobs.values())


async def test_command_keeps_literal_text_and_uses_bounded_versioned_buttons(rig):
    message = make_message(rig.bot, message_thread_id=17)
    await Remind.process(message, MetaInfo(message, text="in 15m tea <&>"), rig.service)
    sent = rig.bot.session.methods[-1]
    assert "tea <&>" in sent.text and sent.parse_mode is None
    assert len(sent.reply_markup.inline_keyboard[0]) == 3
    for row in sent.reply_markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


async def test_callback_rejects_other_owner_and_stale_revision(rig):
    record = await create(rig)
    callback = ReminderCallback.unpack(keyboard(record).inline_keyboard[0][0].callback_data)
    message = make_message(rig.bot, chat={"id": -123, "type": "supergroup"}, message_thread_id=17)
    query = CallbackQuery.model_validate(
        {
            "id": "cb",
            "from": {"id": 43, "first_name": "Other", "is_bot": False},
            "chat_instance": "synthetic",
            "message": message.model_dump(mode="json"),
        },
        context={"bot": rig.bot},
    )
    await Remind.process_cb(query, callback, rig.service)
    assert (await rig.service.get(42, record.key)).value.due_at == record.value.due_at
    query = query.model_copy(update={"from_user": query.from_user.model_copy(update={"id": 42})})
    await Remind.process_cb(query, callback, rig.service)
    await Remind.process_cb(query, callback, rig.service)
    assert (await rig.service.get(42, record.key)).value.due_at == record.value.due_at + timedelta(minutes=10)


async def test_reminder_restart_delivery_and_text_cleanup_use_real_transactions(db):
    from test_feature_postgres import call

    class Backend:
        async def feature_request(self, operation, request):
            return await asyncio.to_thread(call, db, operation, request)

    bot = make_bot()
    now = datetime.now(UTC)
    try:
        store = FeatureStore(Backend())
        service = ReminderService(bot, store, FeatureWorker(store))
        record = await service.create(
            author_id=42,
            author_name="Synthetic owner",
            chat_id=-123,
            thread_id=17,
            source_message_id=123,
            schedule=Schedule(due_at=now + timedelta(days=1), text="Synthetic reminder"),
        )
        db.run("UPDATE msu_hub_private.feature_jobs SET run_at=now()-interval '1 second' WHERE feature='reminders' AND kind='deliver';")
        restarted_store = FeatureStore(Backend())
        worker = FeatureWorker(restarted_store)
        restarted = ReminderService(bot, restarted_store, worker)
        restarted.clock = lambda: now + timedelta(days=3)
        assert await worker.run_once() == 1
        delivered = await restarted.get(42, record.key)
        assert delivered.value.status == "delivered" and len(bot.session.methods) == 1
        assert "опозданием" in bot.session.methods[0].text
        assert await worker.run_once() == 0
        assert db.value("SELECT count(*) FROM msu_hub_private.feature_records WHERE feature='reminders';") == 1
        restarted.clock = lambda: now + timedelta(days=34)
        db.run("UPDATE msu_hub_private.feature_jobs SET run_at=now()-interval '1 second' WHERE feature='reminders' AND kind='cleanup';")
        assert await worker.run_once() == 1
        assert await restarted.list(42) == []
        assert db.value("SELECT count(*) FROM msu_hub_private.feature_jobs WHERE feature='reminders' AND terminal_at IS NULL;") == 0
        assert len(bot.session.methods) == 1
    finally:
        await bot.session.close()

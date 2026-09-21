"""Feedback consent, ownership and durable delivery without external requests."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendDocument, SendMessage
from aiogram.types import BufferedInputFile, InlineKeyboardButton
from pydantic import ValidationError

from msu_hub_bot.feedback import (
    FeedbackAccessDenied,
    FeedbackContext,
    FeedbackDiagnostic,
    FeedbackError,
    FeedbackMessage,
    FeedbackNotFound,
    FeedbackOrigin,
    FeedbackSelection,
    FeedbackService,
)
from msu_hub_bot.feedback import presentation
from msu_hub_bot.feedback.service import DRAFT_RETENTION, MAX_DELIVERY_ATTEMPTS
from msu_hub_bot.storage.errors import RepositoryFailure, RepositoryUnavailable
from msu_hub_bot.storage.features import Conflict, FeatureStore, FeatureWorker
from msu_hub_bot.storage.features import jobs as feature_jobs
from msu_hub_bot.storage.features import store as feature_store
from quiz_helpers import FeatureFixture
from telegram_helpers import RecordingSession, make_message

NOW = datetime(2030, 1, 1, 10, tzinfo=UTC)
DESTINATION = -987654


class FeedbackSession(RecordingSession):
    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        return make_message(
            bot,
            message_id=100 + len(self.methods),
            chat={"id": method.chat_id, "type": "supergroup"},
            from_user={"id": bot.id, "is_bot": True, "first_name": "Bot"},
        )


def restart(rig, *, destination=DESTINATION, reviewer_ids=(99,)):
    rig.store = FeatureStore(rig.backend)
    rig.worker = FeatureWorker(rig.store)
    rig.service = FeedbackService(rig.bot, rig.store, rig.worker, destination_chat_id=destination, reviewer_ids=reviewer_ids)
    rig.service.clock = lambda: rig.backend.now


@pytest.fixture
async def rig(monkeypatch):
    backend = FeatureFixture()
    backend.now = NOW

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return backend.now.replace(tzinfo=None) if tz is None else backend.now.astimezone(tz)

    monkeypatch.setattr(feature_jobs, "datetime", Clock)
    monkeypatch.setattr(feature_store, "datetime", Clock)
    bot = Bot("123456789:" + "a" * 35, session=FeedbackSession())
    value = SimpleNamespace(backend=backend, bot=bot)
    restart(value)
    yield value
    await bot.session.close()


def candidates():
    return FeedbackContext(
        origin=FeedbackOrigin(chat_id=-123, thread_id=17, label="Private synthetic source"),
        reply=FeedbackMessage(
            chat_id=-123, thread_id=17, message_id=2, sent_at=NOW - timedelta(minutes=2), author_name="Reply author", text="Selected reply"
        ),
        recent_messages=[
            FeedbackMessage(
                chat_id=-123,
                thread_id=17,
                message_id=1,
                sent_at=NOW - timedelta(minutes=3),
                author_name="Private author",
                text="UNSELECTED",
            )
        ],
        diagnostics=[
            FeedbackDiagnostic(at=NOW - timedelta(minutes=1), handler="process_roll", command="roll", outcome="failed", message_id=9)
        ],
        diagnostics_since=NOW - timedelta(hours=1),
    )


async def create(rig, *, message_id=20, author_id=42, description="A useful <literal> report", context=None):
    return await rig.service.create(
        author_id=author_id,
        author_name="Reporter",
        chat_id=-123,
        thread_id=17,
        source_message_id=message_id,
        description=description,
        candidates=candidates() if context is None else context,
    )


async def bind(rig, record, *, message_id=100):
    return await rig.service.bind(record.value.author_id, record.key, chat_id=-123, message_id=message_id, expected_etag=record.etag)


def controls(record):
    return {"expected_etag": record.etag, "ui_chat_id": record.value.ui_chat_id, "ui_message_id": record.value.ui_message_id}


async def preview(rig, record=None, **creation):
    record = await bind(rig, await create(rig, **creation)) if record is None else record
    return await rig.service.preview(record.value.author_id, record.key, **controls(record))


async def submit(rig, record=None, **creation):
    record = await preview(rig, **creation) if record is None else record
    return await rig.service.submit(record.value.author_id, record.key, **controls(record))


async def drain(rig):
    for _ in range(12):
        if not await rig.worker.run_once():
            return
    raise AssertionError("Feedback jobs did not become idle")


async def test_repeated_creation_is_idempotent_and_new_command_replaces_only_own_draft(rig):
    first, duplicate = await asyncio.gather(create(rig), create(rig))
    assert first.key == duplicate.key and first.expires_at == NOW + DRAFT_RETENTION
    other = await create(rig, author_id=43)
    newer = await create(rig, message_id=21)
    assert newer.key != first.key
    assert await rig.service.drafts.get(rig.service.scope(42), first.key) is None
    assert await rig.service.drafts.get(rig.service.scope(43), other.key) is not None
    assert rig.backend.jobs == {}


async def test_superseded_or_older_unseen_trigger_cannot_replace_newer_draft(rig):
    original = await create(rig, message_id=20)
    newer = await create(rig, message_id=21)
    restart(rig)
    for message_id in (20, 19):
        with pytest.raises(FeedbackError, match="старый запрос"):
            await create(rig, message_id=message_id)
    assert await rig.service.drafts.get(rig.service.scope(42), original.key) is None
    assert (await rig.service.drafts.get(rig.service.scope(42), newer.key)).value.source_message_id == 21


async def test_delayed_older_creation_checks_all_topics_in_the_same_chat(rig):
    newer = await create(rig, message_id=21)
    other_topic = FeedbackContext(origin=FeedbackOrigin(chat_id=-123, thread_id=99, label="Other topic"))
    with pytest.raises(FeedbackError, match="старый запрос"):
        await rig.service.create(
            author_id=42,
            author_name="Reporter",
            chat_id=-123,
            thread_id=99,
            source_message_id=20,
            description="Older command completed late",
            candidates=other_topic,
        )
    assert await rig.service.drafts.get(rig.service.scope(42), newer.key) is not None


async def test_other_chat_message_ids_are_independent_but_old_cross_chat_replay_stays_rejected(rig):
    first = await create(rig, message_id=20)
    other_context = FeedbackContext(origin=FeedbackOrigin(chat_id=-456, label="Other chat"))
    other = await rig.service.create(
        author_id=42,
        author_name="Reporter",
        chat_id=-456,
        thread_id=None,
        source_message_id=1,
        description="A new command in another chat",
        candidates=other_context,
    )
    with pytest.raises(FeedbackError, match="старый запрос"):
        await create(rig, message_id=20)
    assert await rig.service.drafts.get(rig.service.scope(42), first.key) is None
    assert await rig.service.drafts.get(rig.service.scope(42), other.key) is not None


async def test_creation_memory_has_a_fixed_bounded_day_window(rig):
    first = await create(rig, message_id=20)
    for message_id in range(21, 70):
        await create(rig, message_id=message_id)
    with pytest.raises(FeedbackError, match="50 черновиков"):
        await create(rig, message_id=70)
    rig.backend.now += DRAFT_RETENTION
    current = await create(rig, message_id=70)
    activity = await rig.service.activity.get(rig.service.scope(42), "user:42")
    assert len(activity.value.creations) == 1 and current.key != first.key


async def test_card_binding_is_immutable_and_all_mutations_check_owner_card_and_revision(rig):
    record = await bind(rig, await create(rig))
    assert await bind(rig, record) == record
    with pytest.raises(FeedbackError):
        await bind(rig, record, message_id=101)
    for method in (rig.service.change, rig.service.preview, rig.service.submit, rig.service.cancel):
        with pytest.raises(FeedbackError):
            await method(43, record.key, **controls(record))
        with pytest.raises(FeedbackError):
            await method(42, record.key, **{**controls(record), "ui_message_id": 999})
        with pytest.raises(FeedbackError):
            await method(42, record.key, **{**controls(record), "ui_chat_id": -456})
        with pytest.raises(Conflict):
            await method(42, record.key, **{**controls(record), "expected_etag": "stale"})


async def test_draft_expiry_is_fixed_across_controls_and_restart(rig):
    record = await bind(rig, await create(rig))
    rig.backend.now += timedelta(hours=23)
    record = await rig.service.change(42, record.key, kind="idea", **controls(record))
    assert record.expires_at == NOW + DRAFT_RETENTION
    restart(rig)
    rig.backend.now += timedelta(hours=1)
    with pytest.raises(FeedbackError):
        await rig.service.preview(42, record.key, **controls(record))
    assert not rig.backend.jobs


async def test_submit_requires_preview_and_mutation_invalidates_previous_consent(rig):
    record = await bind(rig, await create(rig))
    with pytest.raises(FeedbackError, match="предпросмотр"):
        await submit(rig, record)
    ready = await preview(rig, record)
    assert ready.value.preview_digest
    changed = await rig.service.change(42, ready.key, kind="idea", **controls(ready))
    assert changed.value.preview_digest is None
    with pytest.raises(Conflict):
        await submit(rig, ready)
    with pytest.raises(FeedbackError, match="предпросмотр"):
        await submit(rig, changed)


async def test_submit_atomically_discards_unselected_context_and_replays_same_report(rig):
    context = candidates()
    context.extra_private = "UNKNOWN_DRAFT_EXTRA"
    context.reply.extra_private = "UNKNOWN_MESSAGE_EXTRA"
    record = await preview(rig, context=context)
    exact = rig.service.build_report(record).rendered_text
    first, repeated = await asyncio.gather(submit(rig, record), submit(rig, record))
    assert first.key == repeated.key and first.value.rendered_text == exact
    assert first.expires_at is None and first.value.status == "queued"
    assert first.value.context.recent_messages == []
    payload = first.value.model_dump_json()
    assert all(canary not in payload for canary in ("UNSELECTED", "Private author", "UNKNOWN_DRAFT_EXTRA", "UNKNOWN_MESSAGE_EXTRA"))
    assert await rig.service.drafts.get(rig.service.scope(42), first.key) is None
    jobs = [job for job in rig.backend.jobs.values() if job["kind"] == "deliver"]
    assert len(jobs) == 1
    commit = next(
        request
        for operation, request in rig.backend.calls
        if operation == "commit" and any(p["collection"] == "reports" for p in request["puts"])
    )
    assert commit["deletes"] == [{"collection": "drafts", "key": record.key}]
    assert len(commit["jobs"]) == 1
    assert {item["collection"] for item in commit["puts"]} == {"reports", "activity", "review_index"}
    assert first.value.review_key is not None
    review = await rig.service.review_index.get(rig.service.scope(42), first.value.review_key)
    assert review.value.report_id == first.key and review.value.status == "new"
    assert "Selected reply" not in review.value.model_dump_json() and "context" not in review.value.model_dump_json()
    with pytest.raises(FeedbackError):
        await create(rig)
    with pytest.raises(FeedbackError):
        await rig.service.submit(42, first.key, **{**controls(record), "ui_message_id": 101})
    with pytest.raises(Conflict):
        await rig.service.submit(42, first.key, **{**controls(record), "expected_etag": "stale"})


async def test_unselected_origin_does_not_survive_as_hidden_source_metadata(rig):
    record = await bind(rig, await create(rig))
    record = await rig.service.change(
        42, record.key, selection=FeedbackSelection(chat=False, reply=False, recent=False, diagnostics=False), **controls(record)
    )
    report = await submit(rig, await preview(rig, record))
    assert report.value.context.origin is None
    assert report.value.context.reply is None and report.value.context.diagnostics_since is None
    assert "-123" not in report.value.model_dump_json()
    assert "source_message_id" not in report.value.model_dump_json()


async def test_changed_renderer_requires_new_preview_and_destination_is_frozen(rig, monkeypatch):
    record = await preview(rig)
    original = presentation.render_report
    monkeypatch.setattr(presentation, "render_report", lambda report: original(report) + "\nNew visible content")
    with pytest.raises(FeedbackError, match="Предпросмотр изменился"):
        await submit(rig, record)
    monkeypatch.setattr(presentation, "render_report", original)
    restart(rig, destination=-222222)
    report = await submit(rig, record)
    assert report.value.destination_chat_id == DESTINATION


async def test_lost_commit_response_retries_exact_submit_transaction(rig):
    record = await preview(rig)
    rig.backend.lose_after_commit = 1
    report = await submit(rig, record)
    calls = [request for operation, request in rig.backend.calls if operation == "commit"]
    assert calls[-1] == calls[-2]
    assert report.value.status == "queued" and len(rig.backend.jobs) == 1


async def test_submit_reconciles_race_between_report_lookup_and_deleted_draft(rig, monkeypatch):
    ready = await preview(rig)
    report = await submit(rig, ready)
    original = rig.service.reports.get
    calls = 0

    async def earlier_snapshot(scope, key):
        nonlocal calls
        calls += 1
        return None if calls == 1 else await original(scope, key)

    monkeypatch.setattr(rig.service.reports, "get", earlier_snapshot)
    assert (await submit(rig, ready)).key == report.key
    assert len(rig.backend.jobs) == 1


async def test_five_submissions_per_hour_survive_restart_without_counting_replays(rig):
    for index in range(5):
        record = await preview(rig, message_id=index + 20)
        first = await submit(rig, record)
        assert (await submit(rig, record)).key == first.key
    restart(rig)
    with pytest.raises(FeedbackError, match="пять"):
        await create(rig, message_id=30)
    rig.backend.now += timedelta(hours=1)
    assert await create(rig, message_id=30)


async def test_cancel_removes_draft_and_never_schedules_a_report(rig):
    record = await preview(rig)
    await rig.service.cancel(42, record.key, **controls(record))
    assert await rig.service.drafts.get(rig.service.scope(42), record.key) is None
    assert not rig.backend.jobs
    with pytest.raises(FeedbackError):
        await submit(rig, record)
    restart(rig)
    with pytest.raises(FeedbackError, match="старый запрос"):
        await create(rig)
    with pytest.raises(FeedbackError, match="старый запрос"):
        await create(rig, message_id=19)


async def test_disabled_destination_and_invalid_description_are_nonfatal(rig):
    restart(rig, destination=0)
    with pytest.raises(FeedbackError, match="не настроена"):
        await create(rig)
    restart(rig)
    for description in ("  ", "x" * 2001):
        with pytest.raises(FeedbackError):
            await create(rig, description=description)
    assert not rig.backend.records


async def test_old_selected_context_cannot_be_silently_retained_on_submit(rig):
    context = candidates()
    context.reply.sent_at = NOW - timedelta(days=30) + timedelta(minutes=1)
    ready = await preview(rig, context=context)
    rig.backend.now += timedelta(minutes=2)
    with pytest.raises(FeedbackError, match="старше 30 дней"):
        await submit(rig, ready)


def test_context_scope_and_byte_budget_reject_cross_topic_or_unbounded_candidates():
    context = candidates().model_dump()
    context["reply"]["thread_id"] = 99
    with pytest.raises(ValidationError):
        FeedbackContext.model_validate(context)
    context = candidates().model_dump()
    context["recent_messages"] = [{**context["reply"], "text": "😀" * 2000} for _ in range(5)]
    with pytest.raises(ValidationError, match="byte budget"):
        FeedbackContext.model_validate(context)


async def test_maximal_unicode_report_stays_within_feature_document_budgets(rig):
    context = candidates()
    context.recent_messages = [
        FeedbackMessage(chat_id=-123, thread_id=17, message_id=index + 1, sent_at=NOW, author_name="😀" * 50, text="😀" * 350)
        for index in range(5)
    ]
    record = await bind(rig, await create(rig, description="😀" * 2000, context=context))
    record = await rig.service.change(42, record.key, selection=FeedbackSelection(recent=True), **controls(record))
    ready = await preview(rig, record)
    report = await submit(rig, ready)
    for value in (ready.value, report.value):
        assert len(json.dumps(value.model_dump(mode="json"), ensure_ascii=False).encode()) < 64 * 1024


async def test_worker_notifies_without_context_and_keeps_permanent_receipt(rig):
    report = await submit(rig)
    await drain(rig)
    saved = await rig.service.get_report(42, report.key)
    assert saved.value.status == "sent" and saved.value.delivered_message_id == 101
    assert saved.expires_at is None
    (method,) = rig.bot.session.methods
    assert isinstance(method, SendMessage)
    assert method.chat_id == DESTINATION and report.key in method.text and method.parse_mode is None
    assert report.value.description in method.text and report.value.author_name in method.text
    assert "Selected reply" not in method.text and "process_roll" not in method.text
    rig.backend.now += timedelta(days=100)
    restart(rig)
    await drain(rig)
    assert (await rig.service.get_report(42, report.key)).value.status == "sent"
    assert len(rig.bot.session.methods) == 1


async def test_oversize_preview_is_complete_file_but_notification_stays_one_short_message(rig):
    context = candidates()
    context.reply.text = "😀" * 700
    report = await submit(rig, description="😀" * 2000, context=context)
    method = presentation.report_method(report.value, -123)
    assert isinstance(method, SendDocument) and isinstance(method.document, BufferedInputFile)
    assert method.document.filename == "report.txt" and method.document.data.decode() == report.value.rendered_text
    await drain(rig)
    (notification,) = rig.bot.session.methods
    assert isinstance(notification, SendMessage) and len(notification.text.encode("utf-16-le")) // 2 <= 4096
    assert (await rig.service.get_report(42, report.key)).value.status == "sent"


async def test_notification_review_button_is_built_from_the_persisted_report_identity(rig):
    report = await submit(rig)
    seen = []

    def button(report_id):
        seen.append(report_id)
        return InlineKeyboardButton(text="Review", url=f"https://example.test/?report={report_id}")

    rig.service.review_button = button
    await drain(rig)
    (method,) = rig.bot.session.methods
    assert seen == [report.key]
    assert method.reply_markup.inline_keyboard[0][0].url.endswith(report.key)


@pytest.mark.parametrize("error_kind", ["timeout", "network"])
async def test_ambiguous_send_is_not_automatically_replayed(rig, monkeypatch, error_kind):
    report = await submit(rig)
    calls = []

    async def lost(bot, method, timeout=None):
        calls.append(method)
        if error_kind == "timeout":
            raise TimeoutError("private upstream details")
        raise TelegramNetworkError(method, "private upstream details")

    monkeypatch.setattr(rig.bot.session, "make_request", lost)
    await drain(rig)
    restart(rig)
    rig.backend.now += timedelta(hours=1)
    await drain(rig)
    saved = await rig.service.get_report(42, report.key)
    assert saved.value.status == "uncertain" and saved.value.failure == "uncertain"
    assert "private upstream" not in saved.value.model_dump_json() and len(calls) == 1


async def test_database_outage_after_send_recovers_without_republishing(rig, monkeypatch):
    report = await submit(rig)
    original = rig.backend.feature_request
    reject_receipt = True

    async def unavailable(operation, request):
        if reject_receipt and operation == "commit" and any(row["payload"].get("status") == "sent" for row in request["puts"]):
            raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        return await original(operation, request)

    monkeypatch.setattr(rig.backend, "feature_request", unavailable)
    await rig.worker.run_once()
    assert (await rig.service.get_report(42, report.key)).value.status == "sending"
    assert len(rig.bot.session.methods) == 1
    reject_receipt = False
    rig.backend.now += timedelta(seconds=90)
    restart(rig)
    await drain(rig)
    assert (await rig.service.get_report(42, report.key)).value.status == "uncertain"
    assert len(rig.bot.session.methods) == 1


async def test_lost_receipt_commit_response_preserves_confirmed_delivery(rig, monkeypatch):
    report = await submit(rig)
    original = rig.backend.feature_request
    receipts = []

    async def lost(operation, request):
        result = await original(operation, request)
        if operation == "commit" and any(row["payload"].get("status") == "sent" for row in request["puts"]):
            receipts.append(request)
            if len(receipts) == 1:
                raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        return result

    monkeypatch.setattr(rig.backend, "feature_request", lost)
    await drain(rig)
    saved = await rig.service.get_report(42, report.key)
    assert saved.value.status == "sent" and saved.value.delivered_message_id == 101
    assert receipts[0] == receipts[1] and len(rig.bot.session.methods) == 1
    rig.backend.now += timedelta(seconds=90)
    restart(rig)
    await drain(rig)
    assert (await rig.service.get_report(42, report.key)).value.status == "sent"
    assert len(rig.bot.session.methods) == 1


async def test_cancelled_send_leaves_marker_for_reconciliation(rig, monkeypatch):
    report = await submit(rig)
    entered = asyncio.Event()
    calls = 0

    async def blocked(bot, method, timeout=None):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(rig.bot.session, "make_request", blocked)
    task = asyncio.create_task(rig.worker.run_once())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await rig.service.get_report(42, report.key)).value.status == "sending"
    rig.backend.now += timedelta(seconds=90)
    restart(rig)
    await drain(rig)
    assert (await rig.service.get_report(42, report.key)).value.status == "uncertain" and calls == 1


async def test_definitive_rejection_stops_and_rate_limit_is_bounded_safe_retry(rig, monkeypatch):
    report = await submit(rig)
    calls = 0

    async def limited(bot, method, timeout=None):
        nonlocal calls
        calls += 1
        raise TelegramRetryAfter(method, "retry later", retry_after=45)

    monkeypatch.setattr(rig.bot.session, "make_request", limited)
    for index in range(MAX_DELIVERY_ATTEMPTS):
        await drain(rig)
        assert calls == index + 1
        rig.backend.now += timedelta(seconds=45)
    saved = await rig.service.get_report(42, report.key)
    assert saved.value.status == "failed" and saved.value.failure == "rate_limit"

    async def rejected(bot, method, timeout=None):
        raise TelegramBadRequest(method, "chat not found")

    monkeypatch.setattr(rig.bot.session, "make_request", rejected)
    second = await submit(rig, message_id=21)
    await drain(rig)
    assert (await rig.service.get_report(42, second.key)).value.failure == "rejected"


async def test_shared_inbox_keeps_owned_records_and_activity_separate(rig):
    mine = await bind(rig, await create(rig, author_id=42))
    theirs = await bind(rig, await create(rig, author_id=43), message_id=101)
    assert mine.scope == theirs.scope and mine.scope.key == "inbox" and mine.scope.owner == "bot"
    with pytest.raises(FeedbackError):
        await rig.service.get(43, mine.key, ui_chat_id=-123, ui_message_id=100)
    mine = await submit(rig, await preview(rig, mine))
    with pytest.raises(FeedbackError):
        await rig.service.get_report(43, mine.key)
    assert await rig.service.drafts.get(theirs.scope, theirs.key) is not None
    activity = await rig.service.activity.list(mine.scope)
    assert {record.key for record in activity} == {"user:42", "user:43"}
    assert {record.value.author_id for record in activity} == {42, 43}


async def test_review_authorization_is_explicit_and_precedes_every_storage_read(rig):
    report = await submit(rig)
    assert rig.service.is_reviewer(99) and not rig.service.is_reviewer(42)
    assert not rig.service.is_reviewer(True)
    rig.backend.calls.clear()
    for user_id in (42, 43, 0, -1):
        with pytest.raises(FeedbackAccessDenied):
            await rig.service.review_list(user_id)
        with pytest.raises(FeedbackAccessDenied):
            await rig.service.review_get(user_id, report.key)
        with pytest.raises(FeedbackAccessDenied):
            await rig.service.review_update(user_id, report.key, expected_etag="wrong", status="done", note="No access")
    assert rig.backend.calls == []
    restart(rig, reviewer_ids=())
    assert not rig.service.is_reviewer(99)
    with pytest.raises(FeedbackAccessDenied):
        await rig.service.review_list(99)


async def test_review_queue_is_newest_first_filterable_and_paginates_without_payload_reads(rig):
    first = await submit(rig, message_id=20)
    rig.backend.now += timedelta(milliseconds=1)
    draft = await bind(rig, await create(rig, message_id=21))
    draft = await rig.service.change(42, draft.key, kind="idea", **controls(draft))
    second = await submit(rig, await preview(rig, draft))
    rig.backend.now += timedelta(milliseconds=1)
    third = await submit(rig, message_id=22)
    _, review = await rig.service.review_get(99, first.key)
    await rig.service.review_update(99, first.key, expected_etag=review.etag, status="done", note="Fixed")
    rig.backend.calls.clear()
    page = await rig.service.review_list(99, limit=2)
    assert [record.value.report_id for record in page] == [third.key, second.key]
    assert len(rig.backend.calls) == 1 and rig.backend.calls[0][0] == "list"
    remainder = await rig.service.review_list(99, after=page[-1].key, limit=2)
    assert [record.value.report_id for record in remainder] == [first.key]
    assert [record.value.report_id for record in await rig.service.review_list(99, kind="idea")] == [second.key]
    assert [record.value.report_id for record in await rig.service.review_list(99, status="new", kind="bug")] == [third.key]
    assert [record.value.report_id for record in await rig.service.review_list(99, status="done")] == [first.key]


async def test_review_index_has_only_bounded_summary_and_detail_retains_exact_selected_snapshot(rig):
    report = await submit(rig, description="A sentence. " * 150)
    detail, review = await rig.service.review_get(99, report.key)
    assert detail.value.rendered_text == report.value.rendered_text
    assert detail.value.context.reply.text == "Selected reply"
    assert review.value.author_name == "Reporter" and len(review.value.summary) == 240 and review.value.summary.endswith("…")
    payload = review.value.model_dump_json()
    assert "Selected reply" not in payload and "process_roll" not in payload and "UNSELECTED" not in payload
    assert "rendered_text" not in payload and "context" not in payload
    with pytest.raises(FeedbackNotFound):
        await rig.service.review_get(99, "0" * 16)
    with pytest.raises(FeedbackNotFound):
        await rig.service.review_get(99, "invalid")


async def test_review_updates_require_exact_review_revision_and_record_reviewer(rig):
    report = await submit(rig)
    original_report, review = await rig.service.review_get(99, report.key)
    rig.backend.now += timedelta(minutes=1)
    updated = await rig.service.review_update(99, report.key, expected_etag=review.etag, status="in_progress", note="Investigating")
    assert updated.value.reviewer_id == 99 and updated.value.reviewed_at == rig.backend.now
    assert updated.value.note == "Investigating" and updated.value.status == "in_progress"
    assert (await rig.service.get_report(42, report.key)).etag == original_report.etag
    with pytest.raises(Conflict):
        await rig.service.review_update(99, report.key, expected_etag=review.etag, status="dismissed", note="Stale")
    with pytest.raises(FeedbackError):
        await rig.service.review_update(99, report.key, expected_etag=updated.etag, status="done", note="x" * 2001)
    assert (await rig.service.review_get(99, report.key))[1].value.note == "Investigating"


async def test_lost_review_commit_response_retries_frozen_update_without_changing_report(rig):
    report = await submit(rig)
    _, review = await rig.service.review_get(99, report.key)
    rig.backend.lose_after_commit = 1
    updated = await rig.service.review_update(99, report.key, expected_etag=review.etag, status="done", note="Resolved")
    requests = [request for operation, request in rig.backend.calls if operation == "commit"]
    assert requests[-1] == requests[-2]
    assert [item["collection"] for item in requests[-1]["puts"]] == ["review_index"]
    assert updated.value.status == "done"
    assert (await rig.service.get_report(42, report.key)).etag == report.etag


async def test_review_update_during_notification_does_not_make_delivery_uncertain(rig, monkeypatch):
    report = await submit(rig)
    _, review = await rig.service.review_get(99, report.key)
    original = rig.bot.session.make_request

    async def reviewing(bot, method, timeout=None):
        assert (await rig.service.get_report(42, report.key)).value.status == "sending"
        await rig.service.review_update(99, report.key, expected_etag=review.etag, status="done", note="Reviewed during send")
        return await original(bot, method, timeout)

    monkeypatch.setattr(rig.bot.session, "make_request", reviewing)
    await drain(rig)
    detail, reviewed = await rig.service.review_get(99, report.key)
    assert detail.value.status == "sent" and detail.value.delivered_message_id == 101
    assert reviewed.value.status == "done" and reviewed.value.note == "Reviewed during send"
    assert len(rig.bot.session.methods) == 1


@pytest.mark.parametrize(
    "parameters", [{"limit": 0}, {"limit": 51}, {"limit": True}, {"after": "invalid"}, {"status": "sending"}, {"kind": "unknown"}]
)
async def test_review_listing_rejects_unbounded_or_invalid_parameters_before_storage(rig, parameters):
    rig.backend.calls.clear()
    with pytest.raises(FeedbackError):
        await rig.service.review_list(99, **parameters)
    assert rig.backend.calls == []

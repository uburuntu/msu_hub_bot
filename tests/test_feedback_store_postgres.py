"""Feedback consent, shared review index and delivery against real PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram import Bot
from telegram_helpers import RecordingSession, make_message
from test_feature_postgres import call

from msu_hub_bot.feedback import FeedbackContext, FeedbackMessage, FeedbackOrigin, FeedbackService
from msu_hub_bot.storage.features import Conflict, FeatureStore, FeatureWorker


@pytest.fixture
async def feedback(application_db):
    class Backend:
        async def feature_request(self, operation, value):
            return await asyncio.to_thread(call, application_db, operation, value)

    class Session(RecordingSession):
        during_send = None

        async def make_request(self, bot, method, timeout=None):
            self.methods.append(method)
            if self.during_send is not None:
                await self.during_send()
            return make_message(bot, message_id=701, chat={"id": method.chat_id, "type": "supergroup"})

    bot = Bot("123456789:" + "a" * 35, session=Session())
    store = FeatureStore(Backend())
    worker = FeatureWorker(store)
    service = FeedbackService(bot, store, worker, destination_chat_id=-987654, reviewer_ids=(99,))
    rig = SimpleNamespace(db=application_db, bot=bot, service=service, worker=worker, now=datetime.now(UTC))
    service.clock = lambda: rig.now
    try:
        yield rig
    finally:
        await bot.session.close()


def controls(record):
    return {"expected_etag": record.etag, "ui_chat_id": record.value.ui_chat_id, "ui_message_id": record.value.ui_message_id}


async def ready(rig, *, author_id=42, kind="bug"):
    context = FeedbackContext(
        origin=FeedbackOrigin(chat_id=-123, label="Synthetic source"),
        reply=FeedbackMessage(
            chat_id=-123, message_id=2, sent_at=rig.now - timedelta(minutes=1), author_name="Replied author", text="Selected reply"
        ),
        recent_messages=[
            FeedbackMessage(
                chat_id=-123, message_id=1, sent_at=rig.now - timedelta(minutes=2), author_name="Other author", text="UNSELECTED"
            )
        ],
    )
    draft = await rig.service.create(
        author_id=author_id,
        author_name=f"Reporter {author_id}",
        chat_id=-123,
        thread_id=None,
        source_message_id=20,
        description=f"Synthetic {kind}\n  report from {author_id}",
        candidates=context,
        kind=kind,
    )
    bound = await rig.service.bind(author_id, draft.key, chat_id=-123, message_id=100, expected_etag=draft.etag)
    return await rig.service.preview(author_id, bound.key, **controls(bound))


async def submit(rig, draft):
    return await rig.service.submit(draft.value.author_id, draft.key, **controls(draft))


async def test_submission_rolls_back_all_records_when_job_insert_fails_then_retries(feedback):
    rig = feedback
    draft = await ready(rig)
    scope = rig.service.scope(42)
    activity = await rig.service.activity.get(scope, "user:42")
    # The disposable constraint fails after the RPC has written the report and
    # index and deleted the draft: none may survive without its delivery job.
    rig.db.run("ALTER TABLE msu_hub_private.feature_jobs ADD CONSTRAINT reject_feedback_job CHECK (feature <> 'feedback');")
    try:
        with pytest.raises(AssertionError, match="reject_feedback_job"):
            await submit(rig, draft)
    finally:
        rig.db.run("ALTER TABLE msu_hub_private.feature_jobs DROP CONSTRAINT reject_feedback_job;")
    assert await rig.service.drafts.get(scope, draft.key) == draft
    assert await rig.service.activity.get(scope, "user:42") == activity
    assert await rig.service.reports.list(scope) == []
    assert await rig.service.review_list(99) == []
    assert rig.db.value("SELECT count(*) FROM msu_hub_private.feature_jobs;") == 0

    report = await submit(rig, draft)
    saved, review = await rig.service.review_get(99, report.key)
    assert saved == report and report.expires_at is None and review.expires_at is None
    assert report.value.context.reply.text == "Selected reply"
    assert report.value.context.recent_messages == [] and "UNSELECTED" not in report.value.rendered_text
    assert review.value.summary == "Synthetic bug report from 42"
    assert await rig.service.drafts.get(scope, draft.key) is None
    assert rig.db.value("""SELECT jsonb_build_array(owner_id,scope_key,kind,record_collection,record_key,state)
        FROM msu_hub_private.feature_jobs WHERE feature='feedback';""") == [999, "inbox", "deliver", "reports", report.key, "pending"]
    assert await submit(rig, draft) == report
    assert rig.db.value("SELECT count(*) FROM msu_hub_private.feature_jobs;") == 1
    assert len(await rig.service.review_list(99)) == 1


async def test_shared_inbox_orders_distinct_authors_newest_first_and_filters_before_paging(feedback):
    rig = feedback
    reports = []
    for author_id, kind in ((42, "bug"), (43, "idea"), (44, "bug")):
        reports.append(await submit(rig, await ready(rig, author_id=author_id, kind=kind)))
        rig.now += timedelta(milliseconds=1)
    newest = await rig.service.review_list(99, limit=2)
    assert [row.value.report_id for row in newest] == [reports[2].key, reports[1].key]
    rest = await rig.service.review_list(99, after=newest[-1].key, limit=2)
    assert [row.value.report_id for row in rest] == [reports[0].key]
    assert {row.value.author_id for row in [*newest, *rest]} == {42, 43, 44}

    changed = await rig.service.review_update(99, reports[2].key, expected_etag=newest[0].etag, status="done", note="Resolved")
    assert changed.key == newest[0].key
    remaining_bug = await rig.service.review_list(99, status="new", kind="bug", limit=1)
    assert [row.value.report_id for row in remaining_bug] == [reports[0].key]
    assert await rig.service.review_list(99, status="new", kind="bug", after=remaining_bug[0].key, limit=1) == []
    assert await rig.service.review_list(99, status="done") == [changed]


async def test_review_cas_survives_inflight_notification_and_preserves_worker_receipt(feedback):
    rig = feedback
    report = await submit(rig, await ready(rig))
    _, initial_review = await rig.service.review_get(99, report.key)
    changes = []

    async def review_while_sending():
        sending, _ = await rig.service.review_get(99, report.key)
        assert sending.value.status == "sending" and sending.etag != report.etag
        changes.append(
            await rig.service.review_update(99, report.key, expected_etag=initial_review.etag, status="in_progress", note="Investigating")
        )
        assert (await rig.service.review_get(99, report.key))[0] == sending

    rig.bot.session.during_send = review_while_sending
    assert await rig.worker.run_once() == 1
    delivered, reviewed = await rig.service.review_get(99, report.key)
    assert len(changes) == 1 and reviewed == changes[0]
    assert delivered.etag != report.etag and delivered.value.status == "sent"
    assert delivered.value.delivered_message_id == 701 and delivered.value.attempts == 1
    assert len(rig.bot.session.methods) == 1
    assert await rig.worker.run_once() == 0

    final = await rig.service.review_update(99, report.key, expected_etag=reviewed.etag, status="done", note="Fixed after notification")
    assert final.etag != reviewed.etag
    with pytest.raises(Conflict):
        await rig.service.review_update(99, report.key, expected_etag=reviewed.etag, status="dismissed", note="Stale browser note")
    assert await rig.service.review_get(99, report.key) == (delivered, final)

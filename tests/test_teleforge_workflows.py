"""Native feedback and worker ownership compose without feature facades."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Router
from aiogram.filters import StateFilter
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from teleforge.app import App
from teleforge.testing import RecordingBot

from msu_hub_bot.commands.feedback import Feedback
from msu_hub_bot.telegram.runtime import Supervisor
from msu_hub_bot.feedback import FeedbackContext, FeedbackOrigin, FeedbackService
from msu_hub_bot.feedback.models import FeedbackDraft
from msu_hub_bot.feedback.presentation import FeedbackCallback
from msu_hub_bot.games.chess_play import service as chess_service
from msu_hub_bot.games.chess_play.service import ChessMatchService
from msu_hub_bot.reminders import ReminderService, Schedule
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker, Record
from quiz_helpers import FeatureFixture

CHAT = -123
TOPIC = 17
AUTHOR = 7


@dataclass
class WorkflowRig:
    backend: FeatureFixture
    bot: RecordingBot
    store: FeatureStore
    worker: FeatureWorker
    feedback: FeedbackService


@pytest.fixture
async def rig():
    backend, bot = FeatureFixture(), RecordingBot()
    backend.now = datetime.now(UTC)
    store = FeatureStore(backend)
    worker = FeatureWorker(store, poll_interval=0.01)
    feedback = FeedbackService(bot, store, worker, destination_chat_id=-987654)
    feedback.clock = lambda: backend.now
    yield WorkflowRig(backend, bot, store, worker, feedback)
    worker.stop()
    await bot.session.close()


def ui(bot: RecordingBot) -> Message:
    return Message(
        message_id=100,
        date=datetime.now(UTC),
        chat=Chat(id=CHAT, type="supergroup"),
        from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
        text="Feedback card",
        message_thread_id=TOPIC,
        is_topic_message=True,
    ).as_(bot)


async def draft(rig: WorkflowRig) -> Record[FeedbackDraft]:
    record = await rig.feedback.create(
        author_id=AUTHOR,
        author_name="Reporter",
        chat_id=CHAT,
        thread_id=TOPIC,
        source_message_id=20,
        description="An actionable report",
        candidates=FeedbackContext(origin=FeedbackOrigin(chat_id=CHAT, thread_id=TOPIC, label="Synthetic chat")),
    )
    return await rig.feedback.bind(AUTHOR, record.key, chat_id=CHAT, message_id=100, expected_etag=record.etag)


def click(rig: WorkflowRig, record: Record[FeedbackDraft], action: str, *, author: int = AUTHOR) -> Update:
    payload = FeedbackCallback(key=record.key, revision=record.etag.replace("-", ""), action=action, value="-")
    return Update(
        update_id=50,
        callback_query=CallbackQuery(
            id="feedback-click",
            from_user=User(id=author, is_bot=False, first_name="Reporter"),
            chat_instance="synthetic",
            message=ui(rig.bot),
            data=payload.pack(),
        ),
    )


def native_app(rig: WorkflowRig) -> App:
    # These callbacks consume an already captured draft and need no archive access.
    app = App(data={"feedback": rig.feedback})
    router = Router()
    router.callback_query.register(Feedback.process_cb, FeedbackCallback.filter(), StateFilter(None))
    app.create_dispatcher().include_router(router)
    return app


@asynccontextmanager
async def running_worker(worker: FeatureWorker) -> AsyncIterator[asyncio.Task[None]]:
    supervisor = Supervisor()
    task = supervisor.create_job(worker.run, trace=False)
    try:
        yield task
    finally:
        worker.stop()
        await supervisor.drain(timeout=2, cancel_timeout=0.5)


async def current(rig: WorkflowRig, key: str) -> Record[FeedbackDraft]:
    return await rig.feedback.get(AUTHOR, key, ui_chat_id=CHAT, ui_message_id=100)


async def eventually(predicate: Callable[[], Awaitable[bool]]) -> None:
    async with asyncio.timeout(2):
        while not await predicate():
            await asyncio.sleep(0)


async def test_exact_preview_then_submit_keeps_real_atomic_report_and_job(rig: WorkflowRig) -> None:
    record = await draft(rig)
    expected = rig.feedback.build_report(record).rendered_text
    async with native_app(rig) as app:
        await app.feed_update(rig.bot, click(rig, record, "p"))
        preview = await current(rig, record.key)
        assert preview.value.preview_digest is not None
        assert isinstance(rig.bot.requests[0], EditMessageText)
        assert rig.bot.requests[0].text == expected
        assert not any(
            button.callback_data and FeedbackCallback.unpack(button.callback_data).action == "s"
            for row in rig.bot.requests[0].reply_markup.inline_keyboard
            for button in row
        )
        await app.feed_update(rig.bot, click(rig, preview, "s"))
        await app.feed_update(rig.bot, click(rig, preview, "s"))
    report = await rig.feedback.get_report(AUTHOR, record.key)
    assert report.value.rendered_text == expected
    jobs = [item for item in rig.backend.jobs.values() if item["kind"] == "deliver"]
    assert len(jobs) == 1 and jobs[0]["generation"] == 1
    assert len([request for request in rig.bot.requests if isinstance(request, AnswerCallbackQuery)]) == 3


async def test_failed_preview_cannot_make_draft_submittable(rig: WorkflowRig) -> None:
    record = await draft(rig)
    rig.bot.recording.responses.append(TimeoutError())
    async with native_app(rig) as app:
        await app.feed_update(rig.bot, click(rig, record, "p"))
        pending = await current(rig, record.key)
        assert pending.value.preview_digest is None
        await app.feed_update(rig.bot, click(rig, pending, "s"))
    assert await rig.feedback.reports.get(rig.feedback.scope(AUTHOR), record.key) is None
    assert not rig.backend.jobs


async def test_committed_submit_survives_failed_ui_refresh_without_second_job(rig: WorkflowRig) -> None:
    record = await draft(rig)
    async with native_app(rig) as app:
        await app.feed_update(rig.bot, click(rig, record, "p"))
        ready = await current(rig, record.key)
        rig.bot.recording.responses.append(TimeoutError())
        await app.feed_update(rig.bot, click(rig, ready, "s"))
        saved = await rig.feedback.get_report(AUTHOR, record.key)
        await app.feed_update(rig.bot, click(rig, ready, "s"))
        replayed = await rig.feedback.get_report(AUTHOR, record.key)
    assert saved.etag == replayed.etag
    assert len([item for item in rig.backend.jobs.values() if item["kind"] == "deliver"]) == 1


async def test_feedback_callback_cannot_borrow_another_authors_preview(rig: WorkflowRig) -> None:
    record = await draft(rig)
    async with native_app(rig) as app:
        await app.feed_update(rig.bot, click(rig, record, "p", author=8))
    assert (await current(rig, record.key)).etag == record.etag
    assert all(isinstance(request, AnswerCallbackQuery) for request in rig.bot.requests)


async def test_worker_lifespan_delivers_existing_reminder_and_stops_before_return(rig: WorkflowRig) -> None:
    reminders = ReminderService(rig.bot, rig.store, rig.worker)
    reminders.clock = lambda: rig.backend.now
    record = await reminders.create(
        author_id=AUTHOR,
        author_name="Reporter",
        chat_id=CHAT,
        thread_id=TOPIC,
        source_message_id=30,
        schedule=Schedule(due_at=rig.backend.now + timedelta(seconds=1), text="A reminder"),
    )
    rig.backend.now += timedelta(seconds=2)

    async def delivered() -> bool:
        return (await reminders.get(AUTHOR, record.key)).value.status == "delivered"

    async with running_worker(rig.worker) as task:
        await eventually(delivered)
    assert task.done()
    assert len(rig.bot.requests) == 1
    assert rig.bot.requests[0].message_thread_id == TOPIC
    assert await rig.worker.run_once() == 0


async def test_worker_preserves_uncertain_chess_publication_without_resending(rig: WorkflowRig, monkeypatch: pytest.MonkeyPatch) -> None:
    matches = ChessMatchService(rig.bot, rig.store, rig.worker)
    matches.clock = lambda: rig.backend.now
    monkeypatch.setattr(chess_service, "render_match", lambda game: b"synthetic chess image")
    message = ui(rig.bot).model_copy(
        update={
            "message_id": 40,
            "from_user": User(id=AUTHOR, is_bot=False, first_name="Player"),
            "text": "/chessplay",
        }
    )
    rig.bot.recording.responses.append(TimeoutError())
    await matches.start(message)
    token = matches.token(rig.bot.id, CHAT, message.message_id)
    before = await matches.get(CHAT, token)
    assert before is not None and before.value.publication == "publishing"
    rig.backend.now += timedelta(minutes=2)

    async def abandoned() -> bool:
        record = await matches.get(CHAT, token)
        return record is not None and record.value.publication == "abandoned"

    async with running_worker(rig.worker):
        await eventually(abandoned)
    after = await matches.get(CHAT, token)
    assert after is not None and after.value.game.status == "finished"
    assert len([request for request in rig.bot.requests if isinstance(request, SendPhoto)]) == 1

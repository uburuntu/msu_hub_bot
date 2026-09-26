"""Native quiz routes use bounded delivery through durable state and worker edits."""

import asyncio
from datetime import timedelta

import pytest
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageCaption, EditMessageMedia, SendPhoto
from aiogram.types import CallbackQuery, Update, User
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from teleforge import delivery
from teleforge.delivery import DeliveryTarget

from msu_hub_bot.games.quiz import QuizService
from msu_hub_bot.providers.wit import Wit
from msu_hub_bot.providers.wolfram import WolframAPI
from msu_hub_bot.routing import build_router
from msu_hub_bot.settings import Settings
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)
from quiz_helpers import rig as rig, score_values, settle


def mount(rig):
    rig.store = FeatureStore(rig.backend)
    rig.worker = FeatureWorker(rig.store)
    rig.quiz = QuizService(rig.bot, rig.store, rig.worker)
    rig.quiz.clock = lambda: rig.backend.now
    dispatcher = Dispatcher(disable_fsm=True, quiz=rig.quiz)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    dispatcher.fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(dispatcher.fsm)
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(SelectiveIsolationMiddleware())
    dispatcher.include_router(build_router(wit=Wit([]), wolfram=WolframAPI(""), config=Settings()))
    return dispatcher


async def open_game(rig, app):
    message = rig.message.model_copy(update={"text": f"/{rig.feature}"})
    await app.feed_update(rig.bot, Update(update_id=1, message=message))
    key = rig.quiz._token(rig.bot.id, message.chat.id, message.message_id)
    record = await rig.quiz.round(rig.feature, message.chat.id, key)
    assert record is not None and record.value.phase == "active"
    return record


async def vote(rig, app, record, choice, actor):
    query = CallbackQuery(
        id=f"vote-{actor}-{choice}",
        chat_instance="synthetic",
        message=rig.session.messages[record.value.message_id],
        from_user=User(id=actor, is_bot=False, first_name=f"Player {actor}"),
        data=f"{rig.feature}:{record.key}:{choice}",
    )
    await app.feed_update(rig.bot, Update(update_id=actor, callback_query=query))


async def test_game_survives_restart_then_deadline_edits_original_ui(rig):
    app = mount(rig)
    record = await open_game(rig, app)
    question = record.value.question
    await asyncio.gather(
        vote(rig, app, record, question.answer, 42),
        vote(rig, app, record, (question.answer + 1) % 6, 43),
    )
    await app.fsm.close()

    # New feature/service/worker objects reload the existing store, with no new provider call.
    replacement = mount(rig)
    restored = await rig.quiz.round(rig.feature, record.value.chat_id, record.key)
    assert restored.value.question == question and restored.value.vote_count == 2
    rig.backend.now += timedelta(minutes=11)
    await settle(rig)
    closed = await rig.quiz.round(rig.feature, record.value.chat_id, record.key)
    assert closed.value.phase == "closed" and closed.value.score_status == "recorded"
    assert await score_values(rig, closed.value.score_day) == {42: 1, 43: 0}
    # A repeated delivery/worker wake does not settle twice or create a replacement photo.
    await vote(rig, replacement, closed, "finish", 42)
    await settle(rig)
    assert await score_values(rig, closed.value.score_day) == {42: 1, 43: 0}
    photos = [method for method in rig.session.methods if isinstance(method, SendPhoto)]
    edits = [method for method in rig.session.methods if isinstance(method, (EditMessageCaption, EditMessageMedia))]
    assert len(photos) == 1 and edits
    assert photos[0].reply_parameters.message_id == rig.message.message_id
    assert photos[0].message_thread_id == 17
    assert all(method.message_id == record.value.message_id for method in edits)
    assert not any(isinstance(method, DeleteMessage) for method in rig.session.methods)
    await replacement.fsm.close()


async def test_failed_worker_edit_keeps_vote_and_repairs_without_second_vote(rig):
    app = mount(rig)
    record = await open_game(rig, app)
    await vote(rig, app, record, record.value.question.answer, 42)
    rig.session.caption_error = True
    await settle(rig)
    unchanged = await rig.quiz.round(rig.feature, record.value.chat_id, record.key)
    assert unchanged.value.vote_count == 1
    rig.session.caption_error = False
    # A later accepted vote schedules a fresh render using authoritative persisted votes.
    await vote(rig, app, record, (record.value.question.answer + 1) % 6, 43)
    await settle(rig)
    repaired = await rig.quiz.round(rig.feature, record.value.chat_id, record.key)
    assert repaired.value.vote_count == 2
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1
    assert any(isinstance(method, AnswerCallbackQuery) for method in rig.session.methods)
    await app.fsm.close()


async def test_cancelled_quiz_waiting_for_delivery_lane_abandons_before_publication(rig, monkeypatch):
    planned = asyncio.Event()
    original = delivery._plan

    async def plan(*args, **kwargs):
        result = await original(*args, **kwargs)
        planned.set()
        return result

    monkeypatch.setattr(delivery, "_plan", plan)
    async with delivery._lane(rig.bot, DeliveryTarget(chat_id=rig.message.chat.id)):
        task = asyncio.create_task(rig.quiz.start(rig.feature, rig.message))
        await asyncio.wait_for(planned.wait(), timeout=2)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    token = rig.quiz._token(rig.bot.id, rig.message.chat.id, rig.message.message_id)
    record = await rig.quiz.round(rig.feature, rig.message.chat.id, token)
    assert record.value.phase == "abandoned"
    assert not any(isinstance(request, SendPhoto) for request in rig.session.methods)
    chat = await rig.quiz.collections[rig.feature].chats.get(record.scope, "state")
    assert chat.value.active is None


async def test_cancelled_quiz_after_api_attempt_keeps_recoverable_publication(rig):
    entered = asyncio.Event()

    async def upload(method):
        entered.set()
        await asyncio.Event().wait()

    rig.session.photo_hook = upload
    task = asyncio.create_task(rig.quiz.start(rig.feature, rig.message))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    token = rig.quiz._token(rig.bot.id, rig.message.chat.id, rig.message.message_id)
    record = await rig.quiz.round(rig.feature, rig.message.chat.id, token)
    assert record.value.phase == "publishing" and record.value.message_id is None
    chat = await rig.quiz.collections[rig.feature].chats.get(record.scope, "state")
    assert chat.value.active == record.key

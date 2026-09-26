"""Feature routers run under Hub's real state middleware and ownership rules."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from aiogram import Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Update
from teleforge import InvocationMiddleware
from teleforge.delivery import DeliveryError
from teleforge.testing import RecordingBot, RecordingSession

from msu_hub_bot.commands import control
from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.features.integration import HubIsolationBridge
from msu_hub_bot.providers.wit import Wit
from msu_hub_bot.providers.wolfram import WolframAPI
from msu_hub_bot.routing import build_router
from msu_hub_bot.settings import Settings
from msu_hub_bot.telemetry import Outcome, failure_outcome, safe_failure
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)
from test_teleforge_reactions import click, open_card, reaction_feature as reaction_feature
from telegram_helpers import make_message


async def test_blocked_delivery_under_native_host_does_not_become_an_unexpected_error_alert(monkeypatch):
    errors_chat, observed = -100999, []
    monkeypatch.setattr(control, "settings", SimpleNamespace(error_chat_id=errors_chat))
    transport = RecordingSession()

    async def respond(bot, method):
        if isinstance(method, SendMessage) and method.chat_id != errors_chat:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        return transport._default(bot, method)

    async def capture(handler, event, data):
        try:
            return await handler(event, data)
        except Exception as error:
            observed.append(error)
            raise

    transport.responder = respond
    bot, executor = RecordingBot(session=transport), TPExecutor(1)
    dispatcher = Dispatcher(disable_fsm=True, cpu_executor=executor)
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(InvocationMiddleware())
    dispatcher.update.outer_middleware(StateContextMiddleware())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.message.middleware(SelectiveIsolationMiddleware())
    dispatcher.message.middleware(capture)
    dispatcher.include_router(build_router(wit=Wit([]), wolfram=WolframAPI(""), config=Settings()))
    try:
        await dispatcher.feed_update(bot, Update(update_id=1, message=make_message(bot, text="/figlet hi")))
        assert len(observed) == 1 and isinstance(observed[0], DeliveryError)
        assert failure_outcome(observed[0]) is Outcome.REJECTED
        assert safe_failure(observed[0])["error.reason"] == "bot_blocked"
        assert not any(getattr(method, "chat_id", None) == errors_chat for method in bot.requests)
        assert observed[0].teleforge_outcome.presentations[0].attempted
    finally:
        await fsm.close()
        executor.shutdown(wait=True)
        await bot.session.close()


@asynccontextmanager
async def embed(app):
    dispatcher = Dispatcher(disable_fsm=True)
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(InvocationMiddleware())
    dispatcher.update.outer_middleware(StateContextMiddleware())
    dispatcher.update.outer_middleware(fsm)
    router = app.build_router()
    for observer in (router.message, router.callback_query):
        observer.middleware(HubIsolationBridge())
        observer.middleware(SelectiveIsolationMiddleware())
    dispatcher.include_router(router)
    try:
        yield dispatcher
    finally:
        await fsm.close()


async def test_real_host_state_scopes_allow_same_user_refresh_coalescing(reaction_feature):
    rig = reaction_feature
    async with embed(rig.app) as dispatcher:
        rig.dispatcher = dispatcher
        dispatcher["db"] = rig.repository
        ui = await open_card(rig)
        started, release = asyncio.Event(), asyncio.Event()
        original = rig.repository.reaction_scoreboard.side_effect

        async def read(chat_id, *, days):
            started.set()
            await release.wait()
            return original(chat_id, days=days)

        rig.repository.reaction_scoreboard.reset_mock(side_effect=False)
        rig.repository.reaction_scoreboard.side_effect = read
        before = len(rig.bot.requests)
        first = asyncio.create_task(dispatcher.feed_update(rig.bot, click(rig, ui, actor=77)))
        await asyncio.wait_for(started.wait(), timeout=2)
        second = asyncio.create_task(dispatcher.feed_update(rig.bot, click(rig, ui, actor=77)))
        try:
            await asyncio.wait_for(second, timeout=2)
            assert not first.done()
            assert len([r for r in rig.bot.requests[before:] if isinstance(r, AnswerCallbackQuery)]) == 2
        finally:
            release.set()
            await first
        assert rig.repository.reaction_scoreboard.await_count == 1
        assert len([r for r in rig.bot.requests[before:] if isinstance(r, EditMessageText)]) == 1

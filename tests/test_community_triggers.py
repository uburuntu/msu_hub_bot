"""Community commands keep their author messages through real routing and services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import BaseMiddleware, Dispatcher
from aiogram.methods import DeleteMessage, DeleteMessages, GetChatMember, SendMessage, SendPhoto
from aiogram.types import Update

from msu_hub_bot.commands.reactions import Reactions
from msu_hub_bot.games import definitions
from msu_hub_bot.games.chess_play import service as chess_service
from msu_hub_bot.games.chess_play.service import ChessMatchService
from msu_hub_bot.games.quiz import QuizService
from msu_hub_bot.games.raffle import RaffleStore
from msu_hub_bot.reminders import ReminderService
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker
from msu_hub_bot.telegram.deletions import MessageDeletions
from msu_hub_bot.telegram.fsm_storage import FeatureFSMStorage
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.web.links import Destination, WebAppLinks
from quiz_helpers import FeatureFixture, GameSession, PHOTO, PNG, PUZZLE
from telegram_helpers import make_message
from test_dispatch_contract import router
from test_reactions_command import scoreboard


class CommunitySession(GameSession):
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetChatMember):
            self.methods.append(method)
            return SimpleNamespace(status="administrator")
        return await super().make_request(bot, method, timeout)


class SelectedHandler(BaseMiddleware):
    async def __call__(self, handler, event, data):
        self.key = data["handler"].flags["handler_key"]
        return await handler(event, data)


@pytest.mark.parametrize("reply", [False, True], ids=["standalone", "reply"])
@pytest.mark.parametrize(
    "command,handler,photo",
    [
        ("/chess", "Chess.process", True),
        ("/CHESS@test_bot", "Chess.process", True),
        ("/chess_top", "Chess.top", False),
        ("/chess_play", "ChessPlay.process", True),
        ("/chess_rating", "ChessRating.process", False),
        ("/geoguess", "Geoguess.process", True),
        ("#geoguess", "Geoguess.process", True),
        ("/geoguess_top", "Geoguess.top", False),
        ("/raffle", "Raffle.process", False),
        ("/розыгрыш", "Raffle.process", False),
        ("/конкурс", "Raffle.process", False),
        ("/remind in 1h synthetic reminder", "Remind.process", False),
        ("/напомни через 1 час чай", "Remind.process", False),
        ("/remind list", "Remind.process", False),
        ("/reactions", "Reactions.process", False),
        ("/реакции", "Reactions.process", False),
        ("/app", "process_app", False),
        ("/start app_launch", "process_app_start", False),
    ],
)
async def test_community_commands_preserve_invocation_and_replied_to_message(monkeypatch, command, handler, photo, reply):
    monkeypatch.setattr(definitions, "random_puzzle", AsyncMock(return_value=PUZZLE))
    monkeypatch.setattr(definitions, "random_photo", AsyncMock(return_value=PHOTO))
    monkeypatch.setattr(definitions, "render_board", Mock(return_value=PNG))
    monkeypatch.setattr(chess_service, "render_match", Mock(return_value=PNG))
    backend = FeatureFixture()
    session = CommunitySession(backend)
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    store = FeatureStore(backend)
    worker = FeatureWorker(store)
    quiz = QuizService(bot, store, worker)
    matches = ChessMatchService(bot, store, worker)
    reminders = ReminderService(bot, store, worker)
    links = WebAppLinks(bot.token, "https://app.example.test")
    links.username = "test_bot"
    source_chat = -1001234567890
    private = handler == "process_app_start"
    if private:
        command = "/start app_" + links.launch(42, Destination(source_chat, 17), now=backend.now)
    message = make_message(
        bot,
        message_id=501,
        date=backend.now,
        text=command,
        chat={"id": 42 if private else source_chat, "type": "private" if private else "supergroup"},
        message_thread_id=None if private else 17,
        is_topic_message=not private,
        reply_to_message=make_message(bot, message_id=401) if reply else None,
    )
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(FeatureFSMStorage(store), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.message.middleware(SelectiveIsolationMiddleware())
    selected = SelectedHandler()
    dispatcher.message.middleware(selected)
    dispatcher.include_router(router())
    dispatcher.workflow_data.update(
        quiz=quiz,
        chess_matches=matches,
        raffles=RaffleStore(bot.id, store),
        reminders=reminders,
        web_apps=links,
        db=SimpleNamespace(reaction_scoreboard=AsyncMock(return_value=scoreboard())),
        deletions=MessageDeletions(store, bot, worker),
    )
    Reactions.permissions.clear()
    try:
        await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=message)))
        assert selected.key == handler
        assert any(isinstance(method, SendPhoto if photo else SendMessage) for method in session.methods)
        protected = {message.message_id, 401} if reply else {message.message_id}
        for method in session.methods:
            if getattr(method, "chat_id", None) != message.chat.id:
                continue
            if isinstance(method, DeleteMessage):
                assert method.message_id not in protected
            elif isinstance(method, DeleteMessages):
                assert not protected.intersection(method.message_ids)
        for job in backend.jobs.values():
            if job["kind"] == "delete_message":
                assert job["key"] not in {f"{message.chat.id}:{message_id}" for message_id in protected}
    finally:
        await fsm.close()
        await bot.session.close()
        Reactions.permissions.clear()

"""Chess buttons coexist with conversations and ratings fail without partial results."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from aiogram.types import CallbackQuery, Update, User

from msu_hub_bot.commands.chess_play import ChessPlay, ChessRating, RatingCallback
from msu_hub_bot.commands.chess_play_view import PlayCallback
from msu_hub_bot.games.chess_play.records import RatedPlayer, RatingPage
from msu_hub_bot.telegram.state import ReleasableEventIsolation, StateContextMiddleware, TopicFSMContextMiddleware
from telegram_helpers import make_bot, make_message
from test_dispatch_contract import Selection, router


async def test_unavailable_store_replies_without_exposing_exception_data():
    bot = make_bot()
    service = AsyncMock()
    service.start.side_effect = RuntimeError("SYNTHETIC_PRIVATE_DETAIL")
    try:
        await ChessPlay.process(make_message(bot), service)
        assert len(bot.session.methods) == 1
        assert "Не удалось открыть" in bot.session.methods[0].text
        assert "SYNTHETIC_PRIVATE_DETAIL" not in bot.session.methods[0].text
        service.start.assert_awaited_once()
    finally:
        await bot.session.close()


@pytest.mark.parametrize("state", ["StickerPack:name", "Prog:code", "Posting:text"])
@pytest.mark.parametrize(
    "data,expected",
    [
        (PlayCallback(game="a" * 12, revision=0, action="join", value="").pack(), "ChessPlay.callback"),
        (RatingCallback(page=1).pack(), "ChessRating.callback"),
    ],
)
async def test_game_buttons_leave_active_draft_unchanged(state, data, expected):
    bot = make_bot()
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.callback_query.middleware(Selection())
    dispatcher.include_router(router())
    message = make_message(bot, message_thread_id=17, is_topic_message=True)
    context = fsm.resolve_context(bot, message.chat.id, 42, thread_id=17)
    await context.set_state(state)
    await context.set_data({"text": "unfinished draft"})
    query = CallbackQuery(id="test", chat_instance="test", from_user=message.from_user, message=message, data=data)
    try:
        handler, _ = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, callback_query=query)))
        assert handler.flags["handler_key"] == expected
        assert await context.get_state() == state
        assert await context.get_data() == {"text": "unfinished draft"}
    finally:
        await fsm.close()
        await bot.session.close()


async def test_rating_failure_does_not_overwrite_existing_page():
    bot = make_bot()
    service = AsyncMock()
    service.rating.side_effect = TimeoutError
    query = CallbackQuery(id="test", chat_instance="test", from_user=make_message(bot).from_user, message=make_message(bot))
    try:
        await ChessRating.callback(query.as_(bot), RatingCallback(page=1), service)
        assert len(bot.session.methods) == 1
        assert isinstance(bot.session.methods[0], AnswerCallbackQuery)
        assert "недоступен" in bot.session.methods[0].text
    finally:
        await bot.session.close()


async def test_rating_labels_and_pages_fit_telegram_limits():
    service = AsyncMock()
    players = tuple(RatedPlayer(user_id=i, name="<&>🧑" * 64, username="u" * 32, rating=800) for i in range(1, 11))
    service.rating.return_value = (RatingPage(players=players, total=11, page=0, pages=2), players[0])
    body, markup = await ChessRating._view(service, User(id=1, is_bot=False, first_name="🧑" * 64))
    text, entities = body.render()
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert len(entities) <= 100
    assert all(e.offset + e.length <= len(text.encode("utf-16-le")) // 2 for e in entities)
    assert len(markup.inline_keyboard[0]) == 1
    assert RatingCallback.unpack(markup.inline_keyboard[0][0].callback_data).page == 1


async def test_unchanged_rating_page_still_acknowledges(monkeypatch):
    bot = make_bot()
    service = AsyncMock()
    player = RatedPlayer(user_id=42, name="User", rating=800)
    service.rating.return_value = (RatingPage(players=(player,), total=1, page=0, pages=1), player)
    request = bot.session.make_request

    async def unchanged(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
        return await request(bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", unchanged)
    query = CallbackQuery(id="test", chat_instance="test", from_user=make_message(bot).from_user, message=make_message(bot))
    try:
        await ChessRating.callback(query.as_(bot), RatingCallback(page=0), service)
        assert isinstance(bot.session.methods[-1], AnswerCallbackQuery)
        assert bot.session.methods[-1].text == ""
    finally:
        await bot.session.close()

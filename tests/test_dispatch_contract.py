"""Replay captured selection policies through native aiogram observers and FSM."""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest
from aiogram import BaseMiddleware, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from common.tg.filters import MetaCommand, SlashCommand
from common.tg.middlewares.settings import SettingsMiddleware
from common.tg.state import ReleasableEventIsolation, SelectiveIsolationMiddleware, StateContextMiddleware, TopicFSMContextMiddleware
from hub_bot.routing import build_router
from hub_bot.utils.wit import Wit
from hub_bot.utils.wolfram import WolframAPI
from msu_hub_bot.settings import Settings
from telegram_helpers import make_bot, make_message

CONTRACT = json.loads((Path(__file__).parent / "fixtures/routing_contract.json").read_text())


class Selection(BaseMiddleware):
    async def __call__(self, handler, event, data):
        selected = data["handler"]
        if selected.flags["handler_key"] == "process_pokakats":
            return await handler(event, data)
        return selected, data.get("meta")


def router():
    return build_router(wit=Wit([]), wolfram=WolframAPI(""), config=Settings(owner_id=7, founder_ids=[8]))


def routes(root, event):
    return [handler for child in root.chain_tail for handler in child.observers[event].handlers]


def aliases(handler):
    for item in handler.filters:
        if isinstance(item.callback, MetaCommand):
            return list(item.callback.parser.commands)
        if isinstance(item.callback, SlashCommand):
            return list(item.callback.commands)
    return []


def is_added_route(handler):
    return aliases(handler) == ["py_stdin", "python_stdin"]


def test_every_route_preserves_order_and_aliases():
    root = router()
    counts = Counter(route["event"] for route in CONTRACT["routes"])
    for kind, count in counts.items():
        actual = routes(root, "error" if kind == "errors" else kind)
        extra = 1 if kind in ("message", "edited_message") else 0
        assert len(actual) == count + extra
        retained = [handler for handler in actual if not is_added_route(handler)]
        expected = [route for route in CONTRACT["routes"] if route["event"] == kind]
        assert len(retained) == len(expected)
        for handler, prior in zip(retained, expected):
            assert handler.callback.__qualname__ == prior["handler"]
            assert aliases(handler) == [alias.lower() for alias in prior["aliases"]]
            assert isinstance(handler.flags["handler_key"], str)
            assert isinstance(handler.flags["fsm_release"], bool)


@pytest.mark.parametrize("case", CONTRACT["cases"], ids=[case["id"] for case in CONTRACT["cases"]])
async def test_captured_selection_through_real_dispatch(case):
    bot = make_bot()
    # Mention matching uses an actual Bot.me() cache without a transport call.
    from aiogram.types import User

    bot._me = User(id=bot.id, is_bot=True, first_name="Synthetic", username="contract_bot")
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.message.middleware(SelectiveIsolationMiddleware())
    dispatcher.message.middleware(Selection())
    dispatcher.include_router(router())
    user = 7 if case.get("actor") == "owner_id" else 8 if case.get("actor") == "founder_ids[0]" else 42
    message = make_message(
        bot,
        from_user=dict(id=user, is_bot=False, first_name="Synthetic"),
        text=case.get("text"),
        caption=case.get("caption"),
        **({"photo": [dict(file_id="photo", file_unique_id="unique", width=1, height=1)]} if "caption" in case else {}),
    )
    if state := case.get("state"):
        context = fsm.resolve_context(bot, message.chat.id, user)
        await context.set_state(state)
    try:
        result = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=message)))
        expected = case.get("approved_target") or case["matched"]
        if expected is None:
            assert result is UNHANDLED
        else:
            handler, meta = result
            assert handler.callback.__qualname__ == expected["handler"]
            if "meta" in case and "approved_target" not in case:
                for key, value in case["meta"].items():
                    assert getattr(meta, key) == value
    finally:
        await fsm.close()
        await bot.session.close()


@pytest.mark.parametrize("command", ["start", "cancel", "beer", "arxiv", "stt"])
async def test_plain_slash_commands_keep_case_and_caption_policy(command):
    bot = make_bot()
    f = SlashCommand(command)
    try:
        assert await f(make_message(bot, text="/" + command.upper()), bot)
        assert not await f(make_message(bot, caption="/" + command.upper()), bot)
    finally:
        await bot.session.close()


async def test_inline_tyan_callback_is_acknowledged_without_chat_preferences():
    bot = make_bot()
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    # No chat exists: preferences must neither access the database nor be required
    # to enter the callback's stale-message guard.
    dispatcher.callback_query.outer_middleware(SettingsMiddleware(None))
    failures = []

    async def capture_error(event):
        failures.append(event.exception)
        return True

    dispatcher.errors.register(capture_error)
    dispatcher.include_router(router())
    update = Update.model_validate(
        {
            "update_id": 1,
            "callback_query": {
                "id": "synthetic",
                "inline_message_id": "synthetic",
                "chat_instance": "synthetic",
                "data": "tyan:sfw:neko",
                "from_user": {"id": 42, "is_bot": False, "first_name": "Synthetic"},
            },
        }
    )
    try:
        await dispatcher.feed_update(bot, update)
        assert failures == []
        assert [method.__api_method__ for method in bot.session.methods] == ["answerCallbackQuery"]
        assert "недоступна" in bot.session.methods[0].text
    finally:
        await fsm.close()
        await bot.session.close()

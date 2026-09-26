"""Native roll/dice stay small while preserving production grammar and isolation."""

import asyncio

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from teleforge import InvocationMiddleware
from teleforge.testing import RecordingBot
from telegram_helpers import make_message

from msu_hub_bot.commands import rolls
from msu_hub_bot.providers.wit import Wit
from msu_hub_bot.providers.wolfram import WolframAPI
from msu_hub_bot.routing import build_router
from msu_hub_bot.settings import Settings
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)


@pytest.fixture
async def runtime():
    bot = RecordingBot(bot_id=123456789)
    dispatcher = Dispatcher(disable_fsm=True)
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(InvocationMiddleware())
    dispatcher.update.outer_middleware(StateContextMiddleware())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.message.middleware(SelectiveIsolationMiddleware())
    dispatcher.include_router(build_router(wit=Wit([]), wolfram=WolframAPI(""), config=Settings()))
    try:
        yield bot, dispatcher
    finally:
        await fsm.close()
        await bot.session.close()


@pytest.mark.parametrize(
    "text,digits",
    [
        ("/roll", 3),
        ("/roll xyz", 3),
        ("/roll -6", 3),
        ("/roll +6", 3),
        ("/roll 6", 6),
        ("/roll 0", 1),
        ("/roll 101", 100),
        ("/roll ١٢", 12),
        ("/ролл 5", 5),
        ("/РОЛЛ@teleforge_test_bot 7", 7),
        ("#ролл_7", 7),
        ("8 #roll", 3),
    ],
)
async def test_roll_grammar_defaults_clamp_and_invocation_target(runtime, monkeypatch, text, digits):
    bot, dispatcher = runtime
    calls = []

    def result(count):
        calls.append(count)
        return "1" * count, "дабл"

    monkeypatch.setattr(rolls, "get_roll", result)
    reply = make_message(bot, message_id=8, text="unrelated source")
    message = make_message(bot, text=text, reply_to_message=reply, is_topic_message=True, message_thread_id=55)

    await dispatcher.feed_update(bot, Update(update_id=1, message=message))

    assert calls == [digits]
    sent = bot.requests[-1]
    assert sent.text == f"<code>{'1' * digits}</code> — дабл"
    assert sent.reply_parameters.message_id == message.message_id and sent.message_thread_id == 55


async def test_roll_keeps_media_caption_trigger(runtime, monkeypatch):
    bot, dispatcher = runtime
    monkeypatch.setattr(rolls, "get_roll", lambda digits: ("1" * digits, ""))
    message = make_message(bot, caption="#roll_4", photo=[{"file_id": "p", "file_unique_id": "p", "width": 20, "height": 20}])

    await dispatcher.feed_update(bot, Update(update_id=1, message=message))

    assert bot.requests[-1].text == "<code>1111</code>"


@pytest.mark.parametrize("index,emoji", [(0, "🎲"), (1, "🎯"), (2, "🏀"), (3, "⚽"), (4, "🎰")])
async def test_dice_retains_all_native_variants(runtime, monkeypatch, index, emoji):
    bot, dispatcher = runtime
    monkeypatch.setattr(rolls.random, "choice", lambda values: values[index])
    message = make_message(bot, text="/dice", is_topic_message=True, message_thread_id=55)

    await dispatcher.feed_update(bot, Update(update_id=1, message=message))

    sent = bot.requests[-1]
    assert sent.__api_method__ == "sendDice" and sent.emoji == emoji
    assert sent.reply_parameters.message_id == message.message_id and sent.message_thread_id == 55


@pytest.mark.parametrize("command", ["roll", "dice"])
async def test_terminal_commands_release_actor_isolation_before_waiting_for_telegram(runtime, command):
    bot, dispatcher = runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(bot, method):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return bot.recording._default(bot, method)

    bot.recording.responder = respond
    first = asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=make_message(bot, text=f"/{command}"))))
    await asyncio.wait_for(entered.wait(), timeout=2)
    second = asyncio.create_task(
        dispatcher.feed_update(bot, Update(update_id=2, message=make_message(bot, message_id=2, text=f"/{command}")))
    )
    try:
        await asyncio.wait_for(second, timeout=2)
        assert not first.done()
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)

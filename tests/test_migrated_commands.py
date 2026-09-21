"""Real simple commands exercise the registered argument and output policies."""

from itertools import cycle
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiogram import Dispatcher
from aiogram.methods import SendDocument, SendMessage
from aiogram.types import Update

from msu_hub_bot.commands import figlet, rolls
from msu_hub_bot.telegram.command_api import register_command
from telegram_helpers import make_bot, make_message


@pytest.fixture
async def bot():
    bot = make_bot()
    yield bot
    await bot.session.close()


async def dispatch(bot, handler, message):
    dispatcher = Dispatcher(disable_fsm=True)
    register_command(dispatcher.message, handler)
    return await dispatcher.feed_update(bot, Update(update_id=1, message=message))


@pytest.mark.parametrize("command_text,digits", [("/roll", 3), ("/ролл bad", 3), ("#roll_6", 6), ("/roll 0", 1), ("/roll 1000", 100)])
async def test_roll_signature_preserves_aliases_defaults_and_formatted_joke(bot, monkeypatch, command_text, digits):
    generate = Mock(return_value=("123", "<смешной & ролл>"))
    monkeypatch.setattr(rolls, "get_roll", generate)
    message = make_message(bot, text=command_text, message_thread_id=17, is_topic_message=True)
    await dispatch(bot, rolls.process_roll, message)
    generate.assert_called_once_with(digits)
    (method,) = bot.session.methods
    assert isinstance(method, SendMessage)
    assert method.text == "123 — <смешной & ролл>" and method.parse_mode is None
    assert [(entity.type, entity.offset, entity.length) for entity in method.entities] == [("code", 0, 3)]
    assert method.message_thread_id == 17 and method.reply_parameters.message_id == message.message_id


@pytest.mark.parametrize("long", [False, True])
async def test_figlet_keeps_complete_rendering_and_uses_file_after_one_message(bot, monkeypatch, long):
    output = "< /\\ & >\n" * (700 if long else 3)
    render = Mock(return_value=output)
    monkeypatch.setattr(figlet, "figlets", cycle([SimpleNamespace(renderText=render)]))
    source = make_message(bot, message_id=10, text="Привет")
    message = make_message(bot, message_id=11, text="/figlet", reply_to_message=source)
    await dispatch(bot, figlet.process_figlet, message)
    render.assert_called_once_with("Privet")
    (method,) = bot.session.methods
    assert method.reply_parameters.message_id == source.message_id
    if long:
        assert isinstance(method, SendDocument)
        assert method.document.data.decode() == output
        assert method.document.filename.endswith(".txt")
    else:
        assert isinstance(method, SendMessage) and method.text == output
        assert method.parse_mode is None and method.entities[0].type == "pre"


async def test_figlet_hard_input_limit_rejects_before_rendering_and_targets_invocation(bot, monkeypatch):
    render = Mock(side_effect=AssertionError("Oversized input must not reach the renderer"))
    monkeypatch.setattr(figlet, "figlets", cycle([SimpleNamespace(renderText=render)]))
    source = make_message(bot, message_id=10, text="x" * 201)
    message = make_message(bot, message_id=11, text="/figlet", reply_to_message=source)
    await dispatch(bot, figlet.process_figlet, message)
    render.assert_not_called()
    (method,) = bot.session.methods
    assert isinstance(method, SendMessage) and method.reply_parameters.message_id == message.message_id
    assert "200" in method.text

"""The /me rewrite keeps the original message unless its replacement was posted."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from msu_hub_bot.commands.other import process_me
from telegram_helpers import make_bot, make_message


def meta(text):
    return SimpleNamespace(extract_text=lambda: (None, text))


@pytest.mark.asyncio
async def test_me_replaces_the_original():
    bot = make_bot()
    message = make_message(bot=bot, text="/me waves")
    await process_me(message, meta("waves <3"))
    send, delete = bot.session.methods
    assert send.__api_method__ == "sendMessage" and send.text.endswith("waves &lt;3")
    assert delete.__api_method__ == "deleteMessage"
    await bot.session.close()


@pytest.mark.asyncio
async def test_me_keeps_the_original_when_sending_is_forbidden(monkeypatch):
    bot = make_bot()
    error = TelegramBadRequest(SendMessage(chat_id=1, text="test"), "Bad Request: not enough rights to send text messages to the chat")
    request = AsyncMock(side_effect=error)
    monkeypatch.setattr(bot.session, "make_request", request)
    assert await process_me(make_message(bot=bot, text="/me waves"), meta("waves")) is True
    assert [call.args[1].__api_method__ for call in request.await_args_list] == ["sendMessage"]
    await bot.session.close()

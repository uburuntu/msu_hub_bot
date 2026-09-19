from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, ErrorEvent, Update
from aiohttp import ClientError

from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.execution.executor import ExecutorBusy
from msu_hub_bot.media.limits import MediaDimensionsError
from msu_hub_bot.telegram.files import DownloadTooLarge
from msu_hub_bot.commands import control
from msu_hub_bot.settings import MissingIntegration, settings
from telegram_helpers import make_bot, make_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected",
    [
        (TimeoutError(), "не успел"),
        (ClientError("transport details"), "связаться"),
        (ExternalServiceError("Попробуйте <ещё>"), "&lt;ещё&gt;"),
        (MissingIntegration("unused"), "не настроена"),
        (ExecutorBusy("transport details"), "несколько задач"),
        (DownloadTooLarge("transport details"), "20 Мб"),
        (MediaDimensionsError("transport details"), "16 Мп"),
    ],
)
async def test_provider_error_replies_are_useful_and_safe(error, expected):
    bot = make_bot()
    session = bot.session
    update = Update(update_id=1, message=make_message(bot=bot))
    assert await control.process_error(ErrorEvent(update=update, exception=error), bot) is True
    method = session.methods[-1]
    assert expected in method.text
    assert "transport details" not in method.text
    await bot.session.close()


@pytest.mark.asyncio
async def test_callback_errors_are_acknowledged():
    bot = make_bot()
    session = bot.session
    query = CallbackQuery.model_validate(
        dict(id="query", from_user=dict(id=4, is_bot=False, first_name="Synthetic"), chat_instance="instance", inline_message_id="inline"),
        context={"bot": bot},
    )
    await control.process_error(ErrorEvent(update=Update(update_id=1, callback_query=query), exception=TimeoutError()), bot)
    assert session.methods[-1].show_alert is True
    await control.process_expired_callback(query)
    assert "заново" in session.methods[-1].text
    await bot.session.close()


@pytest.mark.asyncio
async def test_deleted_message_does_not_trigger_another_error(monkeypatch):
    bot = make_bot()
    session = bot.session
    method = SendMessage(chat_id=1, text="test")
    monkeypatch.setattr(session, "make_request", AsyncMock(side_effect=TelegramBadRequest(method, "message to reply not found")))
    update = Update(update_id=1, message=make_message(bot=bot))
    assert await control.process_error(ErrorEvent(update=update, exception=TimeoutError()), bot) is True
    await bot.session.close()


@pytest.mark.asyncio
async def test_provider_error_redacts_configured_secret(monkeypatch):
    monkeypatch.setattr(settings, "supabase_password", "synthetic-private-password")
    bot = make_bot()
    session = bot.session
    update = Update(update_id=1, message=make_message(bot=bot))
    await control.process_error(ErrorEvent(update=update, exception=ExternalServiceError("failed: synthetic-private-password")), bot)
    assert "synthetic-private-password" not in session.methods[-1].text
    assert "[REDACTED]" in session.methods[-1].text
    await bot.session.close()

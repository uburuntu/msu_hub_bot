"""Wolfram delivery and client lifetimes use synthetic requests only."""

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.methods import DeleteMessage

from msu_hub_bot.settings import MissingIntegration

from msu_hub_bot.providers import wit
from msu_hub_bot.providers import wolfram

METHOD = DeleteMessage(chat_id=42, message_id=1)


def request_message(query="2+2", reply=None):
    progress = SimpleNamespace(delete=AsyncMock())
    message = SimpleNamespace(
        text="/wf " + query,
        caption=None,
        reply_to_message=reply,
        reply=AsyncMock(return_value=progress),
        reply_photo=AsyncMock(),
        reply_document=AsyncMock(),
        reply_video=AsyncMock(),
    )
    return message, progress


@pytest.mark.parametrize("ratio,method", [(1.0, "reply_photo"), (2.1, "reply_photo"), (2.2, "reply_document")])
async def test_result_delivery_uses_text_progress_and_preserves_image_choice(ratio, method):
    message, progress = request_message()
    client = wolfram.WolframAPI("synthetic")
    client.request = AsyncMock(return_value=(io.BytesIO(b"image"), ratio))

    await client.process_wolfram(message)

    client.request.assert_awaited_once_with(query="2+2")
    message.reply.assert_awaited_once_with("🔄 WolframAlpha обрабатывает запрос…")
    getattr(message, method).assert_awaited_once()
    other_method = "reply_document" if method == "reply_photo" else "reply_photo"
    getattr(message, other_method).assert_not_awaited()
    message.reply_video.assert_not_awaited()
    progress.delete.assert_awaited_once()


@pytest.mark.parametrize(
    "failure",
    [
        wolfram.WolframAPIError("[401] synthetic credential failure"),
        wolfram.WolframAPIError("[500] synthetic service failure"),
        aiohttp.ClientError("synthetic connection failure"),
        TimeoutError("synthetic timeout"),
    ],
)
async def test_provider_failure_gets_safe_feedback_and_cleans_progress(failure):
    message, progress = request_message()
    client = wolfram.WolframAPI("synthetic")
    client.request = AsyncMock(side_effect=failure)

    await client.process_wolfram(message)

    assert message.reply.await_count == 2
    feedback = message.reply.await_args.args[0]
    assert "Не удалось получить результат от WolframAlpha" in feedback
    assert "ничего не найдено" not in feedback
    assert "synthetic" not in feedback
    message.reply_photo.assert_not_awaited()
    message.reply_document.assert_not_awaited()
    progress.delete.assert_awaited_once()


@pytest.mark.parametrize("failure", [ValueError("synthetic unexpected failure"), asyncio.CancelledError()])
async def test_unexpected_failure_or_cancellation_cleans_progress_and_propagates(failure):
    message, progress = request_message()
    client = wolfram.WolframAPI("synthetic")
    client.request = AsyncMock(side_effect=failure)

    with pytest.raises(type(failure)):
        await client.process_wolfram(message)

    progress.delete.assert_awaited_once()


async def test_delivery_failure_still_cleans_progress():
    message, progress = request_message()
    client = wolfram.WolframAPI("synthetic")
    client.request = AsyncMock(return_value=(io.BytesIO(b"image"), 1.0))
    message.reply_photo.side_effect = TelegramAPIError(METHOD, "synthetic delivery failure")

    with pytest.raises(TelegramAPIError):
        await client.process_wolfram(message)

    progress.delete.assert_awaited_once()


@pytest.mark.parametrize("failure", [TelegramBadRequest(METHOD, "Message to delete not found"), aiohttp.ClientError(), TimeoutError()])
async def test_cleanup_failure_does_not_erase_a_delivered_result(failure):
    message, progress = request_message()
    client = wolfram.WolframAPI("synthetic")
    client.request = AsyncMock(return_value=(io.BytesIO(b"image"), 1.0))
    message.reply_photo.return_value = "delivered"
    progress.delete.side_effect = failure

    assert await client.process_wolfram(message) == "delivered"


@pytest.mark.parametrize("field", ["text", "caption"])
async def test_query_can_still_come_from_a_replied_message(field):
    reply = SimpleNamespace(text=None, caption=None)
    setattr(reply, field, "integrate x")
    message, progress = request_message(query="", reply=reply)
    client = wolfram.WolframAPI("synthetic")
    client.request = AsyncMock(return_value=(io.BytesIO(b"image"), 1.0))

    await client.process_wolfram(message)

    client.request.assert_awaited_once_with(query="integrate x")
    progress.delete.assert_awaited_once()


async def test_no_query_shows_usage_without_progress_or_provider_call():
    message, progress = request_message(query="")
    client = wolfram.WolframAPI("synthetic")
    client.request = AsyncMock()

    await client.process_wolfram(message)

    assert "Использование:" in message.reply.await_args.args[0]
    client.request.assert_not_awaited()
    progress.delete.assert_not_awaited()


async def test_missing_provider_does_not_send_progress():
    message, progress = request_message()
    client = wolfram.WolframAPI("")

    with pytest.raises(MissingIntegration):
        await client.process_wolfram(message)

    message.reply.assert_not_awaited()
    progress.delete.assert_not_awaited()


@pytest.mark.parametrize("client_class", [wit.WitAPI, wolfram.WolframAPI])
async def test_close_never_creates_an_unused_session(monkeypatch, client_class):
    factory = Mock()
    monkeypatch.setattr(aiohttp, "ClientSession", factory)

    await client_class("synthetic").close()

    factory.assert_not_called()


@pytest.mark.parametrize("client_class", [wit.WitAPI, wolfram.WolframAPI])
async def test_close_closes_an_existing_session(client_class):
    client = client_class("synthetic")
    session = SimpleNamespace(close=AsyncMock())
    client.__dict__["session"] = session

    await client.close()

    session.close.assert_awaited_once()


def test_wolfram_uses_a_client_deadline(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(aiohttp, "ClientSession", factory)
    client = wolfram.WolframAPI("synthetic")

    assert client.session is factory.return_value
    assert factory.call_args.kwargs["timeout"].total == 20

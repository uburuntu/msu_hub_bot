"""Callback acknowledgement, content preferences and one confirmed image send."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendAnimation, SendPhoto
from aiogram.types import CallbackQuery

from msu_hub_bot.commands.tyan import Tyan, TyanCallback
from msu_hub_bot.providers.tyan import TyanImage, TyanUnavailable
from telegram_helpers import make_bot, make_message


def query(bot):
    return CallbackQuery.model_validate(
        {
            "id": "synthetic",
            "chat_instance": "synthetic",
            "from_user": {"id": 42, "is_bot": False, "first_name": "Synthetic"},
            "message": make_message(bot, message_thread_id=17, is_topic_message=True),
        },
        context={"bot": bot},
    )


@pytest.mark.parametrize("animated", [False, True])
async def test_image_keeps_native_kind_credit_topic_reply_and_transport_timeout(monkeypatch, animated):
    bot = make_bot()
    image = TyanImage(
        "https://provider.example/image", animated, "Artist <&>", "https://artist.example", 'https://source.example/?q="one"&id=2'
    )
    request = AsyncMock(return_value=image)
    monkeypatch.setattr(Tyan, "request_tyan", request)
    original = bot.session.make_request
    timeouts = []

    async def send(bot, method, timeout=None):
        if isinstance(method, (SendAnimation, SendPhoto)):
            timeouts.append(timeout)
        return await original(bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", send)
    try:
        await Tyan.process_cb(
            query(bot), TyanCallback(type="sfw", category="hug" if animated else "neko"), SimpleNamespace(with_nsfw=False)
        )
        acknowledgement, sent = bot.session.methods
        assert acknowledgement.__api_method__ == "answerCallbackQuery" and acknowledgement.text is None
        assert isinstance(sent, SendAnimation if animated else SendPhoto)
        assert "Artist &lt;&amp;&gt;" in sent.caption
        assert 'href="https://source.example/?q=&quot;one&quot;&amp;id=2"' in sent.caption
        assert sent.message_thread_id == 17 and sent.reply_parameters.message_id == 1
        assert timeouts == [15]
        request.assert_awaited_once()
    finally:
        await bot.session.close()


@pytest.mark.parametrize("category,enabled", [("neko", False), ("waifu", True), ("trap", True)])
async def test_nsfw_policy_and_unavailable_old_buttons_do_not_fetch(monkeypatch, category, enabled):
    bot = make_bot()
    request = AsyncMock()
    monkeypatch.setattr(Tyan, "request_tyan", request)
    try:
        await Tyan.process_cb(query(bot), TyanCallback(type="nsfw", category=category), SimpleNamespace(with_nsfw=enabled))
        request.assert_not_awaited()
        assert len(bot.session.methods) == 1 and bot.session.methods[0].__api_method__ == "answerCallbackQuery"
        assert ("недоступна" in bot.session.methods[0].text) if enabled else bot.session.methods[0].text == "🚫"
    finally:
        await bot.session.close()


async def test_provider_outage_after_acknowledgement_has_durable_safe_feedback(monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(Tyan, "request_tyan", AsyncMock(side_effect=TyanUnavailable()))
    try:
        await Tyan.process_cb(query(bot), TyanCallback(type="sfw", category="neko"), SimpleNamespace(with_nsfw=False))
        assert [method.__api_method__ for method in bot.session.methods] == ["answerCallbackQuery", "sendMessage"]
        sent = bot.session.methods[-1]
        assert "временно недоступен" in sent.text and sent.message_thread_id == 17 and sent.parse_mode is None
    finally:
        await bot.session.close()


@pytest.mark.parametrize("uncertain", [False, True])
async def test_only_known_media_url_rejection_can_select_another_image(monkeypatch, uncertain):
    bot = make_bot()
    request = AsyncMock(return_value=TyanImage("https://provider.example/image.png", False))
    monkeypatch.setattr(Tyan, "request_tyan", request)
    original = bot.session.make_request
    attempts = []

    async def send(bot, method, timeout=None):
        if isinstance(method, SendPhoto):
            attempts.append(method)
            if len(attempts) == 1:
                if uncertain:
                    raise TelegramNetworkError(method, "Synthetic timeout")
                raise TelegramBadRequest(method, "failed to get HTTP URL content")
        return await original(bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", send)
    try:
        call = Tyan.process_cb(query(bot), TyanCallback(type="sfw", category="neko"), SimpleNamespace(with_nsfw=False))
        if uncertain:
            with pytest.raises(TelegramNetworkError):
                await call
            assert request.await_count == len(attempts) == 1
            assert len(bot.session.methods) == 1
        else:
            await call
            assert request.await_count == len(attempts) == 2
            assert len(bot.session.methods) == 2
    finally:
        await bot.session.close()

"""Synthetic Telegram objects and a recording transport with no network access."""

from datetime import datetime, timezone
from typing import Any, cast

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import File, Message, User


def make_message(bot: Bot | None = None, **fields: Any) -> Message:
    data = {
        "message_id": 1,
        "date": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "chat": {"id": -1001234567890, "type": "supergroup", "title": "Test chat"},
        "from_user": {"id": 42, "is_bot": False, "first_name": "Test user"},
        **fields,
    }
    return Message.model_validate(data, context={"bot": bot})


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.methods: list[TelegramMethod[Any]] = []
        self.closed = False
        self.download_bytes = b"synthetic download"

    async def close(self) -> None:
        self.closed = True

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None) -> TelegramType:
        self.methods.append(method)
        name = method.__api_method__
        if name == "getMe":
            result: object = User(id=123456789, is_bot=True, first_name="Test bot", username="test_bot")
        elif name == "getUpdates":
            result = []
        elif name == "getFile":
            result = File(file_id="file", file_unique_id="unique", file_path="test.txt")
        elif name == "sendMediaGroup":
            result = [make_message(bot, message_id=index + 1) for index, _ in enumerate(method.media)]
        elif name.startswith(("send", "edit", "copy")):
            result = make_message(bot, text=getattr(method, "text", None), entities=getattr(method, "entities", None))
        else:
            result = True
        return cast(TelegramType, result)

    async def stream_content(self, url: str, **kwargs: Any):
        yield self.download_bytes


def make_bot() -> Bot:
    return Bot("123456789:" + "a" * 35, session=RecordingSession())

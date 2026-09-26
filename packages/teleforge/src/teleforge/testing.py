"""Offline native Telegram transport for feature and application tests."""

from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any, cast, get_args

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, InlineKeyboardMarkup, InputFile, Message, User
from pydantic import BaseModel

type Responder = Callable[[Bot, TelegramMethod[Any]], Awaitable[object]]


class RecordingSession(BaseSession):
    """Record actual aiogram methods and uploaded bytes without opening sockets.

    Queued exceptions model failed requests; an async responder can model delays
    or application-specific Telegram results. No unspecified request uses a network.
    """

    def __init__(self, responses: Iterable[object] = (), *, responder: Responder | None = None) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.uploads: list[dict[str, bytes]] = []
        self.responses = deque(responses)
        self.responder = responder
        self.closed = False
        self.next_message_id = 100

    async def close(self) -> None:
        self.closed = True

    async def _uploads(self, bot: Bot, value: object, path: str, output: dict[str, bytes]) -> None:
        if isinstance(value, InputFile):
            output[path] = b"".join([chunk async for chunk in value.read(bot)])
        elif isinstance(value, BaseModel):
            for name in type(value).model_fields:
                await self._uploads(bot, getattr(value, name), f"{path}.{name}" if path else name, output)
            for name, extra in (value.model_extra or {}).items():
                await self._uploads(bot, extra, f"{path}.{name}" if path else name, output)
        elif isinstance(value, list | tuple):
            for index, item in enumerate(value):
                await self._uploads(bot, item, f"{path}.{index}", output)
        elif isinstance(value, dict):
            for name, item in value.items():
                await self._uploads(bot, item, f"{path}.{name}", output)

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if self.closed:
            raise RuntimeError("RecordingSession is closed")
        self.requests.append(method)
        uploads: dict[str, bytes] = {}
        await self._uploads(bot, method, "", uploads)
        self.uploads.append(uploads)
        if self.responses:
            result = self.responses.popleft()
        elif self.responder is not None:
            result = await self.responder(bot, method)
        else:
            result = self._default(bot, method)
        if isinstance(result, BaseException):
            raise result
        return cast(TelegramType, result)

    def _default(self, bot: Bot, method: TelegramMethod[Any]) -> object:
        name = method.__api_method__
        if name == "getMe":
            return User(id=bot.id, is_bot=True, first_name="TeleForge test bot", username="teleforge_test_bot")
        if method.__returning__ is bool:
            return True
        if name.startswith(("send", "editMessage")) and (
            method.__returning__ is Message or Message in get_args(method.__returning__)
        ):
            if getattr(method, "inline_message_id", None):
                return True
            chat_id = getattr(method, "chat_id", None)
            if not isinstance(chat_id, int):
                raise AssertionError("Configure a response for non-numeric test chat IDs")
            message_id = getattr(method, "message_id", None)
            if message_id is None:
                self.next_message_id += 1
                message_id = self.next_message_id
            reply_markup = getattr(method, "reply_markup", None)
            values: dict[str, Any] = {
                "message_id": message_id,
                "date": datetime.now(UTC),
                "chat": Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
                "from_user": User(id=bot.id, is_bot=True, first_name="TeleForge test bot"),
                "text": getattr(method, "text", None),
                "caption": getattr(method, "caption", None),
                "message_thread_id": getattr(method, "message_thread_id", None),
                "is_topic_message": getattr(method, "message_thread_id", None) is not None,
                "business_connection_id": getattr(method, "business_connection_id", None),
                "reply_markup": reply_markup if isinstance(reply_markup, InlineKeyboardMarkup) else None,
            }
            for kind in ("photo", "video", "audio", "document", "animation"):
                replacement = getattr(method, "media", None)
                if getattr(method, kind, None) is not None or getattr(replacement, "type", None) == kind:
                    values[kind] = self._media_result(kind)
                    if replacement is not None:
                        values["caption"] = replacement.caption
            rich = getattr(method, "rich_message", None)
            if rich is not None:
                if rich.blocks is None:
                    raise AssertionError("Configure a response when testing parsed HTML/Markdown rich content")
                blocks: list[dict[str, Any]] = []
                for block in rich.blocks:
                    if block.type in {"photo", "video", "audio", "document", "animation"}:
                        blocks.append({"type": block.type, block.type: self._media_result(block.type)})
                    else:
                        blocks.append(block.model_dump())
                values["rich_message"] = {"blocks": blocks}
            return Message.model_validate(values).as_(bot)
        raise AssertionError(f"Configure an offline response for {name}")

    @staticmethod
    def _media_result(kind: str) -> object:
        values: dict[str, Any] = {"file_id": f"recorded-{kind}", "file_unique_id": f"unique-{kind}"}
        if kind in {"photo", "video", "animation"}:
            values.update(width=100, height=100)
        if kind in {"video", "audio", "animation"}:
            values["duration"] = 1
        return [values] if kind == "photo" else values

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise AssertionError("RecordingSession never fetches remote content")
        yield b""  # pragma: no cover - declare the async-generator protocol


class RecordingBot(Bot):
    """A Bot with the offline recording session installed."""

    def __init__(self, *, session: RecordingSession | None = None, bot_id: int = 42) -> None:
        transport = session if session is not None else RecordingSession()
        super().__init__(token=f"{bot_id}:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", session=transport)
        self.recording = transport

    @property
    def requests(self) -> list[TelegramMethod[Any]]:
        return self.recording.requests

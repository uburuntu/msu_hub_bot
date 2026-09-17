"""Bot presentation helpers and method-aware Telegram request policy."""

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import InputMediaPhoto, InputMediaVideo, Message, ReplyParameters

from msu_hub_bot.telegram.constants import TELEGRAM_CAPTION_MAX_LEN
from msu_hub_bot.telegram.delivery import AlbumMedia, send_album
from msu_hub_bot.telegram.utils import send_super_message
from msu_hub_bot.health import mark_poll_success
from msu_hub_bot.telemetry import Outcome, Telemetry, failure_outcome


class TelegramRequestPolicy(BaseRequestMiddleware):
    """Retry rejected requests and reads; never replay an ambiguous mutation."""

    def __init__(self, attempts: int = 3, max_retry_after: int = 30, *, telemetry: Telemetry | None = None) -> None:
        self.telemetry = telemetry or Telemetry()
        self.attempts = attempts
        self.max_retry_after = max_retry_after

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        try:
            return await self._request(make_request, bot, method)
        except BaseException as error:
            if method.__api_method__ == "getUpdates":
                self.telemetry.record_poll(failure_outcome(error))
            raise

    async def _request(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        reply = getattr(method, "reply_parameters", None)
        allow_missing = getattr(method, "allow_sending_without_reply", None)
        if isinstance(reply, ReplyParameters) and allow_missing is not None:
            # Telegram ignores deprecated top-level fallback when nested reply
            # parameters exist. Keep an explicit per-call override effective.
            method = method.model_copy(update={"reply_parameters": reply.model_copy(update={"allow_sending_without_reply": allow_missing})})
        is_read = method.__api_method__.startswith("get")
        for attempt in range(self.attempts):
            try:
                result = await make_request(bot, method)
            except TelegramRetryAfter as error:
                if attempt + 1 == self.attempts or error.retry_after > self.max_retry_after:
                    raise
                await asyncio.sleep(max(error.retry_after, 0))
            except TelegramNetworkError, TelegramServerError, TimeoutError:
                if not is_read or attempt + 1 == self.attempts:
                    raise
                await asyncio.sleep(2**attempt)
            except TelegramBadRequest as error:
                chat_id = getattr(method, "chat_id", None)
                if (
                    error.message.removeprefix("Bad Request: ").casefold() == "have no rights to send a message"
                    and isinstance(chat_id, int)
                    and chat_id < 0
                ):
                    await bot.leave_chat(chat_id)
                raise
            else:
                if method.__api_method__ == "getUpdates":
                    mark_poll_success()
                    self.telemetry.record_poll(Outcome.SUCCESS)
                return result
        raise AssertionError("A request attempt must return or raise")


@dataclass(slots=True)
class _ChatSend:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class BotWrapper(Bot):
    def __init__(self, token: str, *, session: BaseSession, telemetry: Telemetry | None = None, **kwargs: Any) -> None:
        super().__init__(token, session=session, **kwargs)
        self.session.middleware(TelegramRequestPolicy(telemetry=telemetry))
        self._chat_sends: dict[int, _ChatSend] = {}

    @asynccontextmanager
    async def serial_send(self, chat_id: int) -> AsyncIterator[None]:
        entry = self._chat_sends.setdefault(chat_id, _ChatSend())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if not entry.users:
                del self._chat_sends[chat_id]

    async def send_super_message(
        self,
        text: str,
        web_preview: str | None,
        photos_urls: Iterable[str] | None,
        video_urls: Iterable[str] | None,
        chat_id: int,
        reply_to: int | None = None,
        *,
        message_thread_id: int | None = None,
    ) -> Message | None:
        async with self.serial_send(chat_id):
            return await send_super_message(
                self,
                text,
                web_preview,
                photos_urls,
                video_urls,
                chat_id,
                reply_to,
                message_thread_id=message_thread_id,
            )

    async def send_super_message_prefer_album(
        self,
        text: str,
        web_preview: str | None,
        photos_urls: list[str],
        video_urls: list[str],
        chat_id: int,
        reply_to: int | None = None,
        *,
        message_thread_id: int | None = None,
    ) -> Message | None:
        if len(text) > TELEGRAM_CAPTION_MAX_LEN or not (photos_urls or video_urls):
            if len(photos_urls) + len(video_urls) == 1:
                web_preview = (photos_urls or video_urls)[0]
                photos_urls, video_urls = [], []
            return await self.send_super_message(
                text, web_preview, photos_urls, video_urls, chat_id, reply_to, message_thread_id=message_thread_id
            )
        reply = ReplyParameters(message_id=reply_to) if reply_to is not None else None
        result = None
        async with self.serial_send(chat_id):
            for urls, media_class in ((photos_urls, InputMediaPhoto), (video_urls, InputMediaVideo)):
                media: list[AlbumMedia] = []
                for url in urls:
                    media.append(media_class(media=url, caption=text or None))
                    text = ""
                if media:
                    messages = await send_album(self, chat_id, media, reply_parameters=reply, message_thread_id=message_thread_id)
                    result = messages[0]
        return result

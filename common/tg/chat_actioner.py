"""Topic-aware chat actions whose task lifetime follows the awaited operation."""

import asyncio
from contextlib import suppress
from types import TracebackType

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from common.tg.context import bot_for


class ChatActioner:
    repeat_time = 4.8

    def __init__(self, message: Message, action: str) -> None:
        self.message = message
        self.action = action
        self._task: asyncio.Task[None] | None = None

    async def _loop(self) -> None:
        bot = bot_for(self.message)
        while True:
            await bot.send_chat_action(
                chat_id=self.message.chat.id,
                action=self.action,
                message_thread_id=self.message.message_thread_id if self.message.is_topic_message else None,
            )
            await asyncio.sleep(self.repeat_time)

    async def start(self) -> None:
        await self.stop()
        self._task = asyncio.create_task(self._loop(), name="chat-action")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, TelegramAPIError):
                await task

    async def __aenter__(self) -> "ChatActioner":
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.stop()

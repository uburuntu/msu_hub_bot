from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject
from aiogram.utils.markdown import hbold


class CheckGets(BaseMiddleware):
    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        if isinstance(event, Message) and event.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            number = str(event.message_id)
            if number.endswith(("00000", "11111", "22222", "33333", "44444", "55555", "66666", "77777", "88888")):
                await event.reply(f"🥳 {hbold('Поздравляем')}! Вы отправили сообщение №{hbold(number)}:\n\n— {event.get_url()}")
        return await handler(event, data)

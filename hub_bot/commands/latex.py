from contextlib import suppress

import cachetools
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, InputMediaPhoto

from common.externals.codecogs import Codecogs
from common.tg.filters import MetaInfo


class Latex:
    replies: cachetools.LRUCache[tuple[int, int], int] = cachetools.LRUCache(maxsize=128)
    error_url = Codecogs.url(r"\mathfrak{Invalid\;Equation}")

    @classmethod
    def cache_key(cls, message: Message) -> tuple[int, int]:
        return message.chat.id, message.message_id

    @classmethod
    async def process(cls, message: Message, meta: MetaInfo) -> Message | bool:
        target, text = meta.extract_text()
        if not text:
            return True

        try:
            result = await target.reply_photo(Codecogs.url(text))
        except TelegramBadRequest:
            result = await message.reply_photo(cls.error_url)

        cls.replies[cls.cache_key(target)] = result.message_id
        return result

    @classmethod
    async def process_edited(cls, message: Message, meta: MetaInfo, bot: Bot) -> Message | bool:
        target, text = meta.extract_text()
        if not text:
            return True

        if message_id := cls.replies.get(cls.cache_key(target)):
            with suppress(TelegramBadRequest):
                return await bot.edit_message_media(
                    media=InputMediaPhoto(media=Codecogs.url(text)), chat_id=message.chat.id, message_id=message_id
                )
            return await bot.edit_message_media(media=InputMediaPhoto(media=cls.error_url), chat_id=message.chat.id, message_id=message_id)

        return await cls.process(message, meta)

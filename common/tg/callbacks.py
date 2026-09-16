import asyncio
from datetime import timedelta
from typing import Any

import cachetools
from aiogram.types import Message, CallbackQuery, Chat


class CallbackCommandBase:
    def __init_subclass__(cls, **kwargs: Any) -> None:
        cls.cache = cachetools.LRUCache(maxsize=512)
        cls.locks = cachetools.LRUCache(maxsize=512)

    cache = cachetools.LRUCache(maxsize=512)
    locks = cachetools.LRUCache(maxsize=512)

    cache_time_long_td = timedelta(days=90)
    cache_time_long = int(cache_time_long_td.total_seconds())

    cache_time_10m_td = timedelta(minutes=10)
    cache_time_10m = int(cache_time_10m_td.total_seconds())

    cache_time_10s_td = timedelta(seconds=10)
    cache_time_10s = int(cache_time_10s_td.total_seconds())

    @classmethod
    def cache_key(cls, message: Message) -> tuple:
        return message.chat.id, message.message_id

    @classmethod
    def lock(cls, key) -> asyncio.Lock:
        if isinstance(key, Message):
            key = cls.cache_key(key)
        elif isinstance(key, CallbackQuery):
            key = (key.message.chat.id, key.message.message_id, key.from_user.id)
        elif isinstance(key, Chat):
            key = (key.id,)
        if key in cls.locks:
            lock = cls.locks[key]
        else:
            lock = asyncio.Lock()
            cls.locks[key] = lock
        return lock

    @classmethod
    async def cached_text(cls, message: Message, text_part: str):
        key = cls.cache_key(message)
        if key in cls.cache:
            cls.cache[key] += text_part
            text = cls.cache[key]
        else:
            text = message.html_text + text_part
            cls.cache[key] = text
        return text

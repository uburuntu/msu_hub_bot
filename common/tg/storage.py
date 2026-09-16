"""Application Redis records, separate from aiogram conversation storage."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Mapping
from functools import partial
from typing import cast

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message
from pendulum import DateTime
from redis.asyncio import Redis

from common.tg.runtime import Supervisor
from msu_hub_bot.telemetry import Boundary, Provider, Telemetry

_REMOVE_IF_UNCHANGED = """
if redis.call('HGET', KEYS[1], ARGV[1]) == ARGV[2] then
    return redis.call('HDEL', KEYS[1], ARGV[1])
end
return 0
"""


class RedisStorage:
    """Borrow the application's decoded Redis client; never close it here."""

    def __init__(self, client: Redis, *, prefix: str, supervisor: Supervisor, telemetry: Telemetry | None = None) -> None:
        self.telemetry = telemetry or Telemetry()
        self.client = client
        self.prefix = prefix
        self.supervisor = supervisor
        self._deletions: dict[str, asyncio.Task[None]] = {}

    def generate_key(self, *parts: str | int) -> str:
        return ":".join((self.prefix, *(str(part) for part in parts)))

    async def redis(self) -> Redis:
        return self.client

    async def exists(self, key: str) -> bool:
        return bool(await self.client.exists(key))

    async def get(self, key: str, default: str | None = None) -> str | None:
        value = await self.client.get(key)
        return str(value) if value else default

    async def set(self, key: str, value: str) -> bool:
        return bool(await self.client.set(key, value))

    async def dict_get(self, key: str, field: str, default: str | None = None) -> str | None:
        value = await cast(Awaitable[str | None], self.client.hget(key, field))
        return str(value) if value else default

    async def dict_set(self, key: str, field: str, value: str) -> bool:
        return bool(await cast(Awaitable[int], self.client.hset(key, field, value)))

    async def dict_set_many(self, key: str, values: Mapping[str, str]) -> bool:
        return bool(await cast(Awaitable[int], self.client.hset(key, mapping=dict(values))))

    async def dict_remove(self, key: str, field: str) -> bool:
        return bool(await cast(Awaitable[int], self.client.hdel(key, field)))

    async def dict_all(self, key: str) -> dict[str, str]:
        values = await cast(Awaitable[dict[str, str]], self.client.hgetall(key))
        return {str(field): str(value) for field, value in values.items()}

    async def get_config(self, config: str, default: str = "") -> str:
        return await self.dict_get(self.generate_key("global", "config"), config, default) or default

    async def set_config(self, config: str, value: str) -> bool:
        return await self.dict_set(self.generate_key("global", "config"), config, value)

    async def get_dt(self, config: str, default: DateTime | None = None) -> DateTime | None:
        value = await self.get_config(config)
        return DateTime.fromisoformat(value) if value else default

    async def set_dt(self, config: str, dt: DateTime | None = None) -> bool:
        return await self.set_config(config, (dt or DateTime.now()).isoformat())

    async def mark_message_to_delete_raw(self, chat_id: int, message_id: int, after: int) -> bool:
        key = self.generate_key("bot", "to_delete")
        return await self.dict_set(key, f"{chat_id}_{message_id}", str(int(time.time() + after)))

    async def mark_message_to_delete(self, message: Message, after: int) -> bool:
        return await self.mark_message_to_delete_raw(message.chat.id, message.message_id, after)

    async def _delete(self, bot: Bot, key: str, field: str, scheduled: str, wait: int) -> None:
        await asyncio.sleep(wait)
        if await self.dict_get(key, field) != scheduled:
            return
        chat_id, message_id = map(int, field.split("_"))
        try:
            with self.telemetry.operation(Boundary.PROVIDER, "telegram.delete", provider=Provider.TELEGRAM):
                await bot.delete_message(chat_id, message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        except TelegramAPIError:
            return
        await cast(Awaitable[int], self.client.eval(_REMOVE_IF_UNCHANGED, 1, key, field, scheduled))

    def _deletion_done(self, field: str, task: asyncio.Task[None]) -> None:
        if self._deletions.get(field) is task:
            del self._deletions[field]

    async def process_messages_to_delete(self, bot: Bot) -> bool:
        key = self.generate_key("bot", "to_delete")
        now = int(time.time())
        for field, scheduled in (await self.dict_all(key)).items():
            if field in self._deletions or not re.fullmatch(r"-?[0-9]+_[0-9]+", field):
                continue
            try:
                wait = max(int(scheduled) - now, 0)
            except ValueError:
                continue
            if wait >= 60:
                continue
            task = self.supervisor.create_job(partial(self._delete, bot, key, field, scheduled, wait))
            self._deletions[field] = task
            task.add_done_callback(partial(self._deletion_done, field))
        return True


async def _reset_matching(client: Redis, prefix: str, pattern: re.Pattern[str]) -> int:
    if not prefix or any(character in prefix for character in "*?[]\\"):
        raise ValueError("FSM reset requires a literal nonempty namespace")
    removed = 0
    batch: list[str] = []
    async for key in client.scan_iter(match=f"{prefix}:*", count=500):
        if isinstance(key, str) and pattern.fullmatch(key):
            batch.append(key)
            if len(batch) == 500:
                removed += await client.delete(*batch)
                batch.clear()
    if batch:
        removed += await client.delete(*batch)
    return removed


async def reset_legacy_fsm(client: Redis, *, prefix: str) -> int:
    """Explicit stopped-poller cutover only; never called during ordinary startup."""
    pattern = re.compile(rf"{re.escape(prefix)}:-?[0-9]+:[0-9]+:(?:state|data)")
    return await _reset_matching(client, prefix, pattern)


async def reset_v3_fsm(client: Redis, *, prefix: str) -> int:
    """Discard only this bot's topic conversations for an explicit rollback."""
    if not prefix:
        raise ValueError("FSM reset requires a literal nonempty namespace")
    namespace = f"{prefix}:fsm3"
    pattern = re.compile(rf"{re.escape(namespace)}:[0-9]+:-?[0-9]+:(?:[0-9]+:)?[0-9]+:default:(?:state|data)")
    return await _reset_matching(client, namespace, pattern)

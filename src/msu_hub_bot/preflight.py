"""Read-only production checks. Never starts a Telegram poller or migrates data."""

import asyncio
import shutil
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from typing import cast

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from redis.asyncio import Redis

from msu_hub_bot.storage.factory import create_repository
from msu_hub_bot.storage.features import FeatureStore
from msu_hub_bot.settings import settings


async def check() -> None:
    settings.validate_core()
    async with AsyncExitStack() as stack:
        redis = Redis(host=settings.redis_host, port=settings.redis_port, password=settings.redis_password or None, db=settings.redis_db)
        stack.push_async_callback(redis.aclose)
        database = create_repository(settings)
        stack.push_async_callback(database.close)
        await asyncio.wait_for(cast(Awaitable[bool], redis.ping()), 15)
        await asyncio.wait_for(database.check(), 15)
        await asyncio.wait_for(FeatureStore(database).check(), 15)
        # Match the runtime's HTTP/SOCKS connector and TLS configuration.
        session = AiohttpSession(proxy=settings.proxy or None, timeout=15)
        stack.push_async_callback(session.close)
        bot = Bot(token=settings.bot_token, session=session)
        await asyncio.wait_for(bot.get_me(), 15)
        for program in ("ffmpeg", "ffprobe", "tesseract"):
            if not shutil.which(program):
                raise RuntimeError("Required media program is absent")


if __name__ == "__main__":
    from msu_hub_bot.redaction import install_redaction

    install_redaction()
    try:
        asyncio.run(check())
    except Exception:
        raise SystemExit("Production preflight failed; configuration and dependency checks did not pass") from None
    print("Production preflight passed")

"""Read-only production checks. Never starts a Telegram poller or migrates data."""

import asyncio
import shutil
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from typing import TypedDict, cast

import edgedb
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from redis.asyncio import Redis

from msu_hub_bot.settings import settings


class _TLSOptions(TypedDict, total=False):
    tls_ca: str


async def check() -> None:
    settings.validate_core()
    async with AsyncExitStack() as stack:
        redis = Redis(host=settings.redis_host, port=settings.redis_port, password=settings.redis_password or None, db=settings.redis_db)
        stack.push_async_callback(redis.aclose)
        tls: _TLSOptions = {"tls_ca": settings.edgedb_tls_ca} if settings.edgedb_tls_ca else {}
        database = edgedb.create_async_client(dsn=settings.edgedb_dsn, tls_security=settings.edgedb_tls_security, **tls)
        stack.push_async_callback(database.aclose)
        await asyncio.wait_for(cast(Awaitable[bool], redis.ping()), 15)
        assert await asyncio.wait_for(database.query_single("SELECT 1"), 15) == 1
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

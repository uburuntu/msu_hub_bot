"""Optional uptime ping with a single owned, cancellable producer."""

import asyncio
from contextlib import suppress

import aiohttp


class HealthCheck:
    repeat_time = 60

    def __init__(self, url: str) -> None:
        self.url = url
        self._task: asyncio.Task[None] | None = None

    async def ping(self) -> bytes:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.post(self.url) as response:
                return await response.read()

    async def _loop(self) -> None:
        while True:
            with suppress(TimeoutError, aiohttp.ClientError):
                await self.ping()
            await asyncio.sleep(self.repeat_time)

    async def start(self) -> None:
        if self.url:
            await self.stop()
            self._task = asyncio.create_task(self._loop(), name="uptime-ping")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

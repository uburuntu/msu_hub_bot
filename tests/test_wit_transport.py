"""An interrupted speech POST cannot release sibling request buffers early."""

import asyncio
import io

import aiohttp
import pytest

from msu_hub_bot.providers.wit import Wit


async def test_transport_failure_drains_other_posts_and_closes_every_audio_buffer_without_retry():
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = aiohttp.ClientConnectionError("Synthetic disconnect")
    bodies = []

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        async def __aenter__(self):
            assert not self.body.closed
            if self.body.getvalue() == b"failed":
                raise original
            entered.set()
            try:
                await release.wait()
            finally:
                assert not self.body.closed
                settled.set()
            return self

        async def __aexit__(self, *args):
            assert not self.body.closed

        async def json(self):
            return {"text": "A sibling transcript must not be published alone"}

    class Session:
        def request(self, method, url, *, data, **kwargs):
            assert method == "POST" and isinstance(data, io.BytesIO)
            bodies.append(data)
            return Response(data)

    client = Wit(["synthetic"])
    client.instances[0].__dict__["session"] = Session()
    task = asyncio.create_task(client._recognize_chunks((b"failed", b"sibling")))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not task.done() and all(not body.closed for body in bodies)
        release.set()
        with pytest.raises(aiohttp.ClientConnectionError) as caught:
            await task
        assert caught.value is original and settled.is_set()
        assert len(bodies) == 2 and all(body.closed for body in bodies)
        assert not client._recognition_slots.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)

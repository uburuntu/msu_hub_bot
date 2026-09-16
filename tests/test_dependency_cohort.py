import asyncio
import importlib
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pycares
import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.base import DefaultKeyBuilder, StorageKey
from aiogram.fsm.storage.redis import RedisStorage
from aiohttp_socks import ProxyConnector
from python_socks.async_.asyncio.v2 import Proxy
from redis.asyncio import Redis


@pytest.mark.parametrize("name", ["aiogram", "pydantic_settings", "aiohttp_socks", "redis.asyncio", "bs4", "dns", "common.externals.dvach"])
def test_required_dependency_integrations_import(name):
    importlib.import_module(name)


@pytest.mark.parametrize(
    ("family", "address"),
    [(socket.AF_INET, (b"192.0.2.42", 443)), (socket.AF_INET6, (b"2001:db8::42", 443, 0, 0))],
)
async def test_async_dns_resolver_reaches_native_callback_interface(monkeypatch, family, address):
    calls = []

    def getaddrinfo(channel, host, port, callback, **kwargs):
        calls.append((host, port, kwargs))
        result = SimpleNamespace(nodes=[SimpleNamespace(family=family, addr=address)])
        asyncio.get_running_loop().call_soon(callback, result, None)

    # Keep aiohttp, aiodns and the native channel real; replace only DNS I/O.
    monkeypatch.setattr(pycares.Channel, "getaddrinfo", getaddrinfo)
    resolver = aiohttp.AsyncResolver()
    try:
        results = await resolver.resolve("synthetic.invalid", 443, family=family)
    finally:
        await resolver.close()
    assert len(calls) == 1
    host, port, options = calls[0]
    assert (host, port) == ("synthetic.invalid", 443)
    assert options["family"] == family and options["type"] == socket.SOCK_STREAM
    assert len(results) == 1
    assert results[0]["hostname"] == "synthetic.invalid"
    assert results[0]["host"] == address[0].decode("ascii")
    assert results[0]["port"] == 443 and results[0]["family"] == family


async def test_async_dns_resolver_cancellation_ignores_late_native_callback_and_closes(monkeypatch):
    callbacks = []
    started = asyncio.Event()

    def getaddrinfo(channel, host, port, callback, **kwargs):
        callbacks.append(callback)
        started.set()

    monkeypatch.setattr(pycares.Channel, "getaddrinfo", getaddrinfo)
    resolver = aiohttp.AsyncResolver()
    channel = resolver._resolver._channel
    cancel = Mock(wraps=channel.cancel)
    monkeypatch.setattr(channel, "cancel", cancel)
    task = asyncio.create_task(resolver.resolve("synthetic.invalid", 443))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The real aiodns callback must tolerate native completion after cancellation.
        callbacks[0](None, pycares.errno.ARES_ECANCELLED)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await resolver.close()
    cancel.assert_called_once_with()


async def test_async_dns_resolver_native_numeric_address_without_network():
    resolver = aiohttp.AsyncResolver()
    try:
        # c-ares resolves numeric addresses synchronously without sending DNS packets.
        results = await resolver.resolve("192.0.2.42", 443, family=socket.AF_INET)
    finally:
        await resolver.close()
    assert [(result["host"], result["port"], result["family"]) for result in results] == [("192.0.2.42", 443, socket.AF_INET)]


@pytest.mark.asyncio
async def test_proxy_connector_reaches_the_current_transport_interface_without_network(monkeypatch):
    class InterceptedConnection(Exception):
        pass

    connect = AsyncMock(side_effect=InterceptedConnection)
    monkeypatch.setattr(Proxy, "connect", connect)
    owner = AiohttpSession(proxy="socks5://127.0.0.1:1080")
    session = await owner.create_session()
    try:
        assert isinstance(session.connector, ProxyConnector)
        with pytest.raises(InterceptedConnection):
            await session.get("https://example.org/synthetic", timeout=aiohttp.ClientTimeout(total=1, sock_connect=0.2))
        connect.assert_awaited_once()
        assert connect.call_args.kwargs["dest_host"] == "example.org"
        assert connect.call_args.kwargs["dest_port"] == 443
        assert connect.call_args.kwargs["dest_ssl"] is not None
        assert connect.call_args.kwargs["timeout"] == 0.2
    finally:
        await owner.close()
    assert session.closed


@pytest.mark.asyncio
async def test_redis_client_and_fsm_storage_keep_json_expiry_and_close_contract(monkeypatch):
    client = Redis(host="127.0.0.1", db=15)
    stored = {}

    async def set_value(key, value, **kwargs):
        assert kwargs == {"ex": None}
        stored[key] = value.encode() if isinstance(value, str) else value

    async def get_value(key):
        return stored.get(key)

    monkeypatch.setattr(client, "set", AsyncMock(side_effect=set_value))
    monkeypatch.setattr(client, "get", AsyncMock(side_effect=get_value))
    close = AsyncMock(wraps=client.aclose)
    monkeypatch.setattr(client, "aclose", close)
    storage = RedisStorage(client, key_builder=DefaultKeyBuilder(prefix="synthetic"))
    key = StorageKey(bot_id=101, chat_id=-202, user_id=303, thread_id=404)
    try:
        await storage.set_state(key, "Synthetic:input")
        await storage.set_data(key, {"text": "Тест 😀", "optional": None})
        assert await storage.get_state(key) == "Synthetic:input"
        assert await storage.get_data(key) == {"text": "Тест 😀", "optional": None}
        assert json.loads(stored["synthetic:-202:404:303:data"]) == {"text": "Тест 😀", "optional": None}
    finally:
        await storage.close()
    close.assert_awaited_once_with(close_connection_pool=True)

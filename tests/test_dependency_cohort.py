import importlib
import json
from unittest.mock import AsyncMock

import aiohttp
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

"""The release check uses runtime proxy semantics without polling or mutations."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import GetMe
from aiohttp_socks import ProxyConnector
from python_socks.async_.asyncio.v2 import Proxy

from msu_hub_bot import preflight
from telegram_helpers import RecordingSession


@pytest.fixture
def boundaries(monkeypatch):
    settings = SimpleNamespace(
        validate_core=Mock(),
        redis_host="redis.invalid",
        redis_port=6379,
        redis_password="",
        redis_db=0,
        edgedb_dsn="edgedb://database.invalid",
        edgedb_tls_ca="",
        edgedb_tls_security="strict",
        bot_token="123456789:" + "a" * 35,
        proxy="",
    )
    redis = SimpleNamespace(ping=AsyncMock(return_value=True), aclose=AsyncMock())
    database = SimpleNamespace(query_single=AsyncMock(return_value=1), aclose=AsyncMock())
    monkeypatch.setattr(preflight, "settings", settings)
    monkeypatch.setattr(preflight, "Redis", Mock(return_value=redis))
    monkeypatch.setattr(preflight.edgedb, "create_async_client", Mock(return_value=database))
    monkeypatch.setattr(preflight.shutil, "which", lambda program: "/synthetic/bin/" + program)
    return settings, redis, database


async def test_preflight_only_gets_identity_and_closes_all_clients(boundaries, monkeypatch):
    _, redis, database = boundaries
    session = RecordingSession()
    factory = Mock(return_value=session)
    monkeypatch.setattr(preflight, "AiohttpSession", factory)
    await preflight.check()
    factory.assert_called_once_with(proxy=None, timeout=15)
    assert [type(method) for method in session.methods] == [GetMe]
    database.query_single.assert_awaited_once_with("SELECT 1")
    assert session.closed
    redis.aclose.assert_awaited_once()
    database.aclose.assert_awaited_once()


async def test_socks_preflight_uses_runtime_connector_without_network(boundaries, monkeypatch):
    settings, redis, database = boundaries
    settings.proxy = "socks5://proxy.invalid:1080"
    sessions = []

    class InterceptedConnection(Exception):
        pass

    def factory(**kwargs):
        session = AiohttpSession(**kwargs)
        sessions.append(session)
        return session

    connect = AsyncMock(side_effect=InterceptedConnection)
    monkeypatch.setattr(Proxy, "connect", connect)
    monkeypatch.setattr(preflight, "AiohttpSession", factory)
    with pytest.raises(InterceptedConnection):
        await preflight.check()
    connect.assert_awaited_once()
    assert connect.call_args.kwargs["dest_host"] == "api.telegram.org"
    assert connect.call_args.kwargs["dest_port"] == 443
    assert connect.call_args.kwargs["dest_ssl"] is not None
    assert sessions[0]._connector_type is ProxyConnector
    assert sessions[0]._session.closed
    redis.aclose.assert_awaited_once()
    database.aclose.assert_awaited_once()


@pytest.mark.parametrize("failure", [RuntimeError("synthetic provider failure"), asyncio.CancelledError()])
async def test_preflight_failure_or_cancellation_closes_every_resource(boundaries, monkeypatch, failure):
    _, redis, database = boundaries
    session = RecordingSession()
    monkeypatch.setattr(preflight, "AiohttpSession", lambda **kwargs: session)
    monkeypatch.setattr(preflight.Bot, "get_me", AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await preflight.check()
    assert session.closed
    redis.aclose.assert_awaited_once()
    database.aclose.assert_awaited_once()


async def test_preflight_partial_initialization_closes_redis(boundaries, monkeypatch):
    _, redis, _ = boundaries
    monkeypatch.setattr(preflight.edgedb, "create_async_client", Mock(side_effect=ValueError("synthetic DSN error")))
    with pytest.raises(ValueError):
        await preflight.check()
    redis.aclose.assert_awaited_once()


async def test_preflight_close_failure_does_not_skip_other_cleanup(boundaries, monkeypatch):
    _, redis, database = boundaries
    database.aclose.side_effect = RuntimeError("synthetic cleanup failure")
    session = RecordingSession()
    monkeypatch.setattr(preflight, "AiohttpSession", lambda **kwargs: session)
    with pytest.raises(RuntimeError):
        await preflight.check()
    assert session.closed
    redis.aclose.assert_awaited_once()


async def test_preflight_telegram_deadline_closes_the_session(boundaries, monkeypatch):
    _, redis, database = boundaries
    session = RecordingSession()
    wait_for = asyncio.wait_for

    async def bounded(awaitable, timeout):
        assert timeout == 15
        return await wait_for(awaitable, 0.02)

    async def delayed(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(preflight, "asyncio", SimpleNamespace(wait_for=bounded))
    monkeypatch.setattr(preflight, "AiohttpSession", lambda **kwargs: session)
    monkeypatch.setattr(preflight.Bot, "get_me", delayed)
    with pytest.raises(TimeoutError):
        await preflight.check()
    assert session.closed
    redis.aclose.assert_awaited_once()
    database.aclose.assert_awaited_once()

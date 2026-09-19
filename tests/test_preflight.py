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
        bot_token="123456789:" + "a" * 35,
        proxy="",
    )
    database = SimpleNamespace(check=AsyncMock(), close=AsyncMock(), feature_request=AsyncMock(return_value={"version": 1}))
    monkeypatch.setattr(preflight, "settings", settings)
    monkeypatch.setattr(preflight, "create_repository", Mock(return_value=database))
    monkeypatch.setattr(preflight.shutil, "which", lambda program: "/synthetic/bin/" + program)
    return settings, database


async def test_preflight_only_gets_identity_and_closes_all_clients(boundaries, monkeypatch):
    _, database = boundaries
    session = RecordingSession()
    factory = Mock(return_value=session)
    monkeypatch.setattr(preflight, "AiohttpSession", factory)
    await preflight.check()
    factory.assert_called_once_with(proxy=None, timeout=15)
    assert [type(method) for method in session.methods] == [GetMe]
    database.check.assert_awaited_once_with()
    database.feature_request.assert_awaited_once_with("health", {})
    assert session.closed
    database.close.assert_awaited_once()


async def test_socks_preflight_uses_runtime_connector_without_network(boundaries, monkeypatch):
    settings, database = boundaries
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
    database.close.assert_awaited_once()


@pytest.mark.parametrize("failure", [RuntimeError("synthetic provider failure"), asyncio.CancelledError()])
async def test_preflight_failure_or_cancellation_closes_every_resource(boundaries, monkeypatch, failure):
    _, database = boundaries
    session = RecordingSession()
    monkeypatch.setattr(preflight, "AiohttpSession", lambda **kwargs: session)
    monkeypatch.setattr(preflight.Bot, "get_me", AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await preflight.check()
    assert session.closed
    database.close.assert_awaited_once()


async def test_preflight_partial_initialization_does_not_allocate_telegram(boundaries, monkeypatch):
    session = Mock()
    monkeypatch.setattr(preflight, "AiohttpSession", session)
    monkeypatch.setattr(preflight, "create_repository", Mock(side_effect=ValueError("synthetic configuration error")))
    with pytest.raises(ValueError):
        await preflight.check()
    session.assert_not_called()


async def test_preflight_close_failure_does_not_skip_other_cleanup(boundaries, monkeypatch):
    _, database = boundaries
    database.close.side_effect = RuntimeError("synthetic cleanup failure")
    session = RecordingSession()
    monkeypatch.setattr(preflight, "AiohttpSession", lambda **kwargs: session)
    with pytest.raises(RuntimeError):
        await preflight.check()
    assert session.closed


async def test_preflight_telegram_deadline_closes_the_session(boundaries, monkeypatch):
    _, database = boundaries
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
    database.close.assert_awaited_once()

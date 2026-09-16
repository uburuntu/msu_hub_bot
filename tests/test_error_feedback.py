import ast
import traceback
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiogram
import pytest
from aiogram.utils.markdown import hbold, hpre, quote_html
from aiohttp import ClientError

from common.externals.exceptions import ExternalServiceError
from msu_hub_bot.redaction import redact
from msu_hub_bot.settings import MissingIntegration, settings


@pytest.fixture
def handlers():
    tree = ast.parse(Path("hub_bot/main.py").read_text())
    tree.body = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name in {"process_error", "reply_error", "process_expired_callback"}
    ]
    namespace = dict(
        aiogram=aiogram,
        ClientError=ClientError,
        MissingIntegration=MissingIntegration,
        ExternalServiceError=ExternalServiceError,
        Update=object,
        CallbackQuery=object,
        suppress=suppress,
        redact=redact,
        logger=Mock(),
        settings=settings,
        quote_html=quote_html,
        hbold=hbold,
        hpre=hpre,
        traceback=traceback,
        errors={},
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    exec(compile(tree, "<error-feedback>", "exec"), namespace)
    return namespace


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected",
    [
        (TimeoutError(), "не успел"),
        (ClientError("transport details"), "связаться"),
        (ExternalServiceError("Попробуйте <ещё>"), "&lt;ещё&gt;"),
        (MissingIntegration("unused"), "не настроена"),
    ],
)
async def test_provider_error_replies_are_useful_and_safe(handlers, error, expected):
    message = SimpleNamespace(reply=AsyncMock())
    update = SimpleNamespace(callback_query=None, message=message)
    assert await handlers["process_error"](update, error) is True
    assert expected in message.reply.call_args.args[0]
    assert "transport details" not in message.reply.call_args.args[0]


@pytest.mark.asyncio
async def test_callback_errors_are_acknowledged(handlers):
    query = SimpleNamespace(answer=AsyncMock())
    await handlers["process_error"](SimpleNamespace(callback_query=query), TimeoutError())
    assert query.answer.call_args.kwargs["show_alert"] is True
    query.answer.reset_mock()
    await handlers["process_expired_callback"](query)
    assert "заново" in query.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_deleted_message_does_not_trigger_another_error(handlers):
    message = SimpleNamespace(reply=AsyncMock(side_effect=aiogram.exceptions.MessageToReplyNotFound("Message to reply not found")))
    update = SimpleNamespace(callback_query=None, message=message)
    assert await handlers["process_error"](update, TimeoutError()) is True


@pytest.mark.asyncio
async def test_provider_error_redacts_configured_secret(handlers, monkeypatch):
    monkeypatch.setattr(settings, "redis_password", "synthetic-private-password")
    message = SimpleNamespace(reply=AsyncMock())
    update = SimpleNamespace(callback_query=None, message=message)
    await handlers["process_error"](update, ExternalServiceError("failed: synthetic-private-password"))
    assert "synthetic-private-password" not in message.reply.call_args.args[0]
    assert "[REDACTED]" in message.reply.call_args.args[0]

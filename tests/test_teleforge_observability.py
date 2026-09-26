"""Handled acquisition errors remain rejected work in the host's diagnostics."""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Dispatcher
from aiogram.types import Update
from teleforge import App, Feature, InvocationMiddleware, TextInput
from teleforge.testing import RecordingBot

from msu_hub_bot.features.command import command, format_input_error
from msu_hub_bot.feedback.context import DiagnosticBuffer
from msu_hub_bot.telegram.middlewares.telemetry import HandlerTelemetryMiddleware
from msu_hub_bot.telemetry import Telemetry
from telegram_helpers import make_message
from telemetry_helpers import Capture, config


@pytest.mark.parametrize("body,expected", [("", "rejected"), ("hello", "success")])
async def test_host_observes_handled_guidance_without_mistaking_it_for_success(monkeypatch, body, expected):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    calls = []

    class Echo(Feature, key="echo"):
        @command("echo", text=TextInput())
        async def echo(self, text: str) -> str:
            calls.append(text)
            return text

    sink = Capture()
    telemetry = Telemetry(config(), {"echo.echo"}, transport=sink)
    telemetry.register_commands({"echo"})
    diagnostics = DiagnosticBuffer()
    bot = RecordingBot()
    message = make_message(bot, text=f"/echo {body}")
    app = App(Echo(), input_formatter=format_input_error)
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(InvocationMiddleware())
    dispatcher.message.middleware(HandlerTelemetryMiddleware(telemetry, diagnostics=diagnostics))
    dispatcher.include_router(app.build_router())
    await telemetry.start()
    try:
        await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    finally:
        await telemetry.close()
        await bot.session.close()
    assert len(bot.requests) == 1
    assert calls == ([body] if body else [])
    outcomes = [{entry.key: entry.value.string_value for entry in span.attributes}["outcome"] for span in sink.spans()]
    assert outcomes == [expected]
    feedback_message = message.model_copy(update={"message_id": message.message_id + 1})
    saved = diagnostics.snapshot(feedback_message, before=datetime.now(UTC) + timedelta(seconds=1))
    assert len(saved) == 1
    assert saved[0].outcome == ("completed" if body else "failed")

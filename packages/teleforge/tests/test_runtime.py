import asyncio
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from teleforge.app import AdmissionClosed, App, DrainTimeout
from teleforge.context import Context
from teleforge.declarations import command
from teleforge.feature import Feature
from teleforge.testing import RecordingBot


def update() -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=7, type="private"),
            from_user=User(id=7, is_bot=False, first_name="Synthetic"),
            text="/run",
        ),
    )


class Work(Feature):
    def __init__(self) -> None:
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.closed = False
        self.finished = False

    @asynccontextmanager
    async def lifespan(self, app: App) -> AsyncIterator[None]:
        try:
            yield
        finally:
            self.closed = True

    @command("run")
    async def run(self, ctx: Context) -> None:
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            assert not self.closed
            self.finished = True


async def test_shutdown_drains_native_dispatch_before_closing_feature_resources() -> None:
    feature, bot = Work(), RecordingBot()
    app = App(feature)
    task = asyncio.create_task(app.feed_update(bot, update()))
    await feature.entered.wait()
    closing = asyncio.create_task(app.aclose())
    await asyncio.sleep(0)
    assert not feature.closed
    assert app._dispatcher is not None
    with pytest.raises(AdmissionClosed):
        await app._dispatcher.feed_update(bot, update())
    feature.release.set()
    await task
    await closing
    assert feature.finished and feature.closed


async def test_drain_timeout_cancels_and_joins_before_closing() -> None:
    feature, bot = Work(), RecordingBot()
    app = App(feature, drain_timeout=0, cancel_timeout=1)
    task = asyncio.create_task(app.feed_update(bot, update()))
    await feature.entered.wait()
    await app.aclose()
    assert task.cancelled() and feature.finished and feature.closed


async def test_cancellation_resistant_update_keeps_resources_open() -> None:
    class Resistant(Work):
        async def run(self, ctx: Context) -> None:
            self.entered.set()
            while not self.release.is_set():
                with suppress(asyncio.CancelledError):
                    await self.release.wait()
            assert not self.closed

    feature, bot = Resistant(), RecordingBot()
    app = App(feature, drain_timeout=0, cancel_timeout=0.01)
    task = asyncio.create_task(app.feed_update(bot, update()))
    await feature.entered.wait()
    with pytest.raises(DrainTimeout):
        await app.aclose()
    assert not feature.closed
    feature.release.set()
    await task
    await app.aclose()
    assert feature.closed


async def test_handler_cannot_deadlock_by_closing_its_own_application() -> None:
    class SelfClose(Feature):
        @command("run")
        async def run(self, ctx: Context) -> None:
            await app.aclose()

    app = App(SelfClose())
    with pytest.raises(RuntimeError, match="own application"):
        await app.feed_update(RecordingBot(), update())
    await app.aclose()


async def test_polling_session_closes_only_after_successful_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    feature, bot = Work(), RecordingBot()
    app = App(feature)
    dispatcher = app.create_dispatcher()

    async def polling(selected: Bot, **options: object) -> None:
        assert options["close_bot_session"] is False
        await app.start()

    monkeypatch.setattr(dispatcher, "start_polling", polling)
    await app.run_polling(bot, close_bot_session=True)
    assert feature.closed and bot.recording.closed


async def test_completed_update_does_not_keep_its_callers_later_work_admitted() -> None:
    feature, bot = Work(), RecordingBot()
    app = App(feature, drain_timeout=1, cancel_timeout=0)
    after = asyncio.Event()

    async def caller() -> None:
        await app.feed_update(bot, update())
        await after.wait()

    task = asyncio.create_task(caller())
    await feature.entered.wait()
    closing = asyncio.create_task(app.aclose())
    await asyncio.sleep(0)
    feature.release.set()
    await closing
    assert not task.done() and feature.closed
    after.set()
    await task


async def test_configuration_is_frozen_while_startup_is_suspended() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def resource() -> AsyncIterator[object]:
        entered.set()
        await release.wait()
        yield object()

    app = App().resource(resource)
    starting = asyncio.create_task(app.start())
    await entered.wait()
    with pytest.raises(RuntimeError, match="resources before"):
        app.resource(resource)
    with pytest.raises(RuntimeError, match="Include features before"):
        app.include(Work())
    release.set()
    await starting
    await app.aclose()


async def test_shutdown_before_startup_cannot_reopen_resources() -> None:
    feature, bot = Work(), RecordingBot()
    app = App(feature)
    await app.aclose()
    with pytest.raises(AdmissionClosed):
        await app.start()
    with pytest.raises(AdmissionClosed):
        await app.feed_update(bot, update())
    assert app._stack is None and app._dispatcher is None


@pytest.mark.parametrize("transport", ["polling", "webhook", "background-webhook"])
async def test_native_returned_request_remains_admitted_through_delivery(transport: str) -> None:
    feature, bot = Work(), RecordingBot()
    app = App(feature)
    dispatcher = app.create_dispatcher()
    entered, release = asyncio.Event(), asyncio.Event()

    # A native route deliberately bypasses the TeleForge binding adapter.
    @dispatcher.message()
    async def native(message: Message) -> SendMessage:
        return message.answer("native response")

    async def responder(selected: Bot, method: object) -> object:
        entered.set()
        await release.wait()
        assert not feature.closed
        return True

    bot.recording.responder = responder
    await app.start()
    with warnings.catch_warnings(record=True):
        if transport == "polling":
            task = asyncio.create_task(dispatcher._process_update(bot, update()))
        else:
            task = asyncio.create_task(
                dispatcher.feed_webhook_update(bot, update(), _timeout=0 if transport == "background-webhook" else 55)
            )
        await asyncio.wait_for(entered.wait(), 1)
        if transport == "background-webhook":
            assert await task is None
        closing = asyncio.create_task(app.aclose())
        await asyncio.sleep(0)
        assert not closing.done() and not feature.closed
        release.set()
        result = await task
        await closing
        await asyncio.sleep(0)
    assert result is (True if transport == "polling" else None)
    assert len(bot.requests) == 1 and feature.closed


async def test_native_returned_request_is_cancelled_before_resources_close() -> None:
    feature, bot = Work(), RecordingBot()
    app = App(feature, drain_timeout=0, cancel_timeout=1)
    dispatcher = app.create_dispatcher()
    entered, stopped = asyncio.Event(), asyncio.Event()

    @dispatcher.message()
    async def native(message: Message) -> SendMessage:
        return message.answer("native response")

    async def responder(selected: Bot, method: object) -> object:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not feature.closed
            stopped.set()

    bot.recording.responder = responder
    await app.start()
    task = asyncio.create_task(dispatcher._process_update(bot, update()))
    await asyncio.wait_for(entered.wait(), 1)
    await app.aclose()
    assert task.cancelled() and stopped.is_set() and feature.closed

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from typing import Any

import pytest
from aiogram import Bot, Router
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.types import Update
from test_runtime import update

from teleforge import App, Context, DrainTimeout, Feature, command
from teleforge.testing import RecordingBot, RecordingSession


class PollingSession(RecordingSession):
    def __init__(self, history: list[str], updates: list[Update] | None = None) -> None:
        super().__init__(responder=self.respond)
        self.history = history
        self.updates = updates
        self.entered = asyncio.Event()
        self.cancelling = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.cancel_release.set()
        self.polling_task: asyncio.Task[Any] | None = None
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()

    async def respond(self, bot: Bot, method: TelegramMethod[Any]) -> object:
        if not isinstance(method, GetUpdates):
            return self._default(bot, method)
        if self.updates is not None:
            updates, self.updates = self.updates, None
            return updates
        self.polling_task = asyncio.current_task()
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelling.set()
            await self.cancel_release.wait()
            self.history.append("poller:stopped")

    async def close(self) -> None:
        self.close_entered.set()
        await self.close_release.wait()
        self.history.append("session:closed")
        await super().close()


class Owned(Feature):
    def __init__(self, history: list[str]) -> None:
        self.history = history
        self.closed = False
        self.close_error: Exception | None = None

    @asynccontextmanager
    async def lifespan(self, app: App) -> AsyncIterator[None]:
        self.history.append("feature:started")
        try:
            yield
        finally:
            self.closed = True
            self.history.append("feature:closed")
            if self.close_error is not None:
                raise self.close_error


def assert_native_joined(app: App, session: PollingSession) -> None:
    assert session.polling_task is not None and session.polling_task.done()
    dispatcher = app._dispatcher
    assert dispatcher is not None
    assert not dispatcher._running_lock.locked()
    assert not dispatcher._stop_signal._waiters
    assert not dispatcher._stopped_signal._waiters


@pytest.mark.parametrize("owned_session", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_native_polling_owner_joins_transport_before_resources_close(
    owned_session: bool, cancelled: bool
) -> None:
    history: list[str] = []
    feature, session = Owned(history), PollingSession(history)
    bot = RecordingBot(session=session)
    app = App(feature)
    running = asyncio.create_task(app.run_polling(bot, handle_signals=False, close_bot_session=owned_session))
    await asyncio.wait_for(session.entered.wait(), 1)
    if cancelled:
        running.cancel("owner stopped")
        with pytest.raises(asyncio.CancelledError, match="owner stopped"):
            await running
    else:
        assert app._dispatcher is not None
        await app._dispatcher.stop_polling()
        await running
    assert_native_joined(app, session)
    assert feature.closed and session.closed is owned_session
    assert history == ["feature:started", "poller:stopped", "feature:closed"] + (
        ["session:closed"] if owned_session else []
    )


@pytest.mark.parametrize("phase", ["feature", "router"])
async def test_cancellation_during_native_startup_unwinds_without_starting_a_poller(phase: str) -> None:
    history: list[str] = []
    entered, stopped = asyncio.Event(), asyncio.Event()
    startup_task = None

    async def pending() -> None:
        nonlocal startup_task
        startup_task = asyncio.current_task()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
            history.append("startup:stopped")

    class Starting(Owned):
        @asynccontextmanager
        async def lifespan(self, app: App) -> AsyncIterator[None]:
            async with super().lifespan(app):
                await pending()
                yield

    feature = Starting(history) if phase == "feature" else Owned(history)
    session = PollingSession(history)
    app = App(feature)
    dispatcher = app.create_dispatcher()
    if phase == "router":
        router = Router()
        router.startup.register(pending)
        dispatcher.include_router(router)
    running = asyncio.create_task(
        app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    )
    await asyncio.wait_for(entered.wait(), 1)
    running.cancel("startup stopped")
    with pytest.raises(asyncio.CancelledError, match="startup stopped"):
        await running
    assert stopped.is_set() and startup_task is not None and startup_task.done()
    assert feature.closed and session.closed and session.requests == []
    assert not dispatcher._running_lock.locked() and not dispatcher._stop_signal._waiters
    assert history == ["feature:started", "startup:stopped", "feature:closed", "session:closed"]


async def test_repeated_owner_cancellation_does_not_interrupt_native_join_or_session_close() -> None:
    history: list[str] = []
    feature, session = Owned(history), PollingSession(history)
    session.cancel_release.clear()
    session.close_release.clear()
    app = App(feature)
    running = asyncio.create_task(
        app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    )
    await asyncio.wait_for(session.entered.wait(), 1)
    running.cancel("first cancellation")
    await asyncio.wait_for(session.cancelling.wait(), 1)
    running.cancel("second cancellation")
    await asyncio.sleep(0)
    assert not running.done() and not feature.closed and not session.closed
    session.cancel_release.set()
    await asyncio.wait_for(session.close_entered.wait(), 1)
    assert_native_joined(app, session)
    running.cancel("third cancellation")
    await asyncio.sleep(0)
    assert not running.done() and feature.closed and not session.closed
    session.close_release.set()
    with pytest.raises(asyncio.CancelledError, match="first cancellation"):
        await running
    assert session.closed and history == ["feature:started", "poller:stopped", "feature:closed", "session:closed"]


async def test_startup_callback_cannot_suppress_cancellation_and_start_a_new_poller() -> None:
    history: list[str] = []
    entered = asyncio.Event()
    feature, session = Owned(history), PollingSession(history)
    app = App(feature)
    dispatcher = app.create_dispatcher()

    async def startup() -> None:
        entered.set()
        with suppress(asyncio.CancelledError):
            await asyncio.Event().wait()

    dispatcher.startup.register(startup)
    running = asyncio.create_task(
        app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    )
    await asyncio.wait_for(entered.wait(), 1)
    running.cancel("startup stopped")
    with pytest.raises(asyncio.CancelledError, match="startup stopped"):
        await asyncio.wait_for(running, 1)
    assert feature.closed and session.closed and session.requests == []
    assert not dispatcher._running_lock.locked() and not dispatcher._stop_signal._waiters


async def test_native_error_remains_primary_when_resource_cleanup_also_fails() -> None:
    history: list[str] = []
    feature, session = Owned(history), PollingSession(history)
    native_error = ConnectionError("native getMe failure")
    feature.close_error = RuntimeError("resource cleanup failure")
    session.responses.append(native_error)
    app = App(feature)
    with pytest.raises(ConnectionError) as caught:
        await app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    assert caught.value is native_error
    assert caught.value.__cause__ is feature.close_error
    assert feature.closed and not session.closed
    assert app._dispatcher is not None
    assert not app._dispatcher._running_lock.locked()
    assert not app._dispatcher._stop_signal._waiters


async def test_native_startup_failure_unwinds_owned_resources_and_session() -> None:
    history: list[str] = []
    startup_error = RuntimeError("startup failed")

    class Broken(Owned):
        @asynccontextmanager
        async def lifespan(self, app: App) -> AsyncIterator[None]:
            async with super().lifespan(app):
                raise startup_error
                yield

    feature, session = Broken(history), PollingSession(history)
    app = App(feature)
    with pytest.raises(RuntimeError) as caught:
        await app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    assert caught.value is startup_error and session.requests == []
    assert feature.closed and session.closed and app._fsm_closed
    assert history == ["feature:started", "feature:closed", "session:closed"]


async def test_startup_error_remains_primary_when_partial_resource_cleanup_fails() -> None:
    startup_error = ConnectionError("second resource startup failed")
    cleanup_error = RuntimeError("first resource cleanup failed")

    @asynccontextmanager
    async def first() -> AsyncIterator[object]:
        try:
            yield object()
        finally:
            raise cleanup_error

    @asynccontextmanager
    async def second() -> AsyncIterator[object]:
        raise startup_error
        yield object()

    session = PollingSession([])
    app = App().resource(first).resource(second)
    with pytest.raises(ConnectionError) as caught:
        await app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    assert caught.value is startup_error
    assert caught.value.__cause__ is cleanup_error
    assert session.requests == [] and session.closed
    assert app._fsm_closed


async def test_owner_cancellation_joins_failed_startup_resource_cleanup(caplog: pytest.LogCaptureFixture) -> None:
    startup_error = ConnectionError("second resource startup failed")
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    history: list[str] = []

    @asynccontextmanager
    async def first() -> AsyncIterator[object]:
        try:
            yield object()
        finally:
            entered.set()
            await release.wait()
            finished.set()
            history.append("resource:closed")

    @asynccontextmanager
    async def second() -> AsyncIterator[object]:
        raise startup_error
        yield object()

    session = PollingSession(history)
    app = App().resource(first).resource(second)
    running = asyncio.create_task(
        app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    )
    await asyncio.wait_for(entered.wait(), 1)
    running.cancel("owner stopped during failed startup cleanup")
    try:
        await asyncio.sleep(0)
        assert not running.done() and not session.closed and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError, match="owner stopped during failed startup cleanup") as caught:
        await running
    assert caught.value.__cause__ is startup_error
    assert finished.is_set() and session.closed and session.requests == []
    assert history == ["resource:closed", "session:closed"]
    assert app._fsm_closed
    assert not any("exception in shielded future" in record.message for record in caplog.records)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_polling_resources_enter_and_exit_in_the_same_task_and_context(cancelled: bool) -> None:
    local: ContextVar[str | None] = ContextVar("polling_resource", default=None)
    owner = None
    history: list[str] = []

    @asynccontextmanager
    async def resource() -> AsyncIterator[object]:
        nonlocal owner
        owner = asyncio.current_task()
        token = local.set("active")
        try:
            yield object()
        finally:
            assert asyncio.current_task() is owner and local.get() == "active"
            local.reset(token)
            history.append("resource:closed")

    session = PollingSession(history)
    app = App().resource(resource)
    running = asyncio.create_task(
        app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    )
    await asyncio.wait_for(session.entered.wait(), 1)
    if cancelled:
        running.cancel("owner stopped")
        with pytest.raises(asyncio.CancelledError, match="owner stopped"):
            await running
    else:
        assert app._dispatcher is not None
        await app._dispatcher.stop_polling()
        await running
    assert owner is not None and owner.done()
    assert local.get() is None and session.closed
    assert history == ["poller:stopped", "resource:closed", "session:closed"]


async def test_cancellation_during_normal_session_cleanup_is_preserved_after_join() -> None:
    history: list[str] = []
    feature, session = Owned(history), PollingSession(history)
    session.close_release.clear()
    app = App(feature)
    running = asyncio.create_task(
        app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    )
    await asyncio.wait_for(session.entered.wait(), 1)
    assert app._dispatcher is not None
    await app._dispatcher.stop_polling()
    await asyncio.wait_for(session.close_entered.wait(), 1)
    running.cancel("cleanup cancellation")
    await asyncio.sleep(0)
    assert_native_joined(app, session)
    assert not running.done() and feature.closed and not session.closed
    session.close_release.set()
    with pytest.raises(asyncio.CancelledError, match="cleanup cancellation"):
        await running
    assert session.closed


async def test_native_shutdown_failure_cannot_leave_stop_waiter_alive() -> None:
    history: list[str] = []
    feature, session = Owned(history), PollingSession(history)
    app = App(feature)
    dispatcher = app.create_dispatcher()
    shutdown_error = RuntimeError("native shutdown callback failed")

    async def fail_shutdown() -> None:
        raise shutdown_error

    dispatcher.shutdown.register(fail_shutdown)
    running = asyncio.create_task(
        app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    )
    await asyncio.wait_for(session.entered.wait(), 1)
    running.cancel("owner stopped")
    with pytest.raises(asyncio.CancelledError, match="owner stopped") as caught:
        await running
    assert caught.value.__cause__ is shutdown_error
    assert_native_joined(app, session)
    assert feature.closed and session.closed


async def test_cancelled_owner_preserves_drain_timeout_and_leaves_resources_open() -> None:
    history: list[str] = []
    entered, release = asyncio.Event(), asyncio.Event()
    update_task = None

    class Resistant(Owned):
        @command("run")
        async def run(self, ctx: Context) -> None:
            nonlocal update_task
            update_task = asyncio.current_task()
            entered.set()
            while not release.is_set():
                with suppress(asyncio.CancelledError):
                    await release.wait()
            assert not self.closed

    feature, session = Resistant(history), PollingSession(history, [update()])
    app = App(feature, drain_timeout=0, cancel_timeout=0.01)
    running = asyncio.create_task(
        app.run_polling(RecordingBot(session=session), handle_signals=False, close_bot_session=True)
    )
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(session.entered.wait(), 1)
    running.cancel("owner stopped")
    with pytest.raises(asyncio.CancelledError, match="owner stopped") as caught:
        await running
    assert isinstance(caught.value.__cause__, DrainTimeout)
    assert_native_joined(app, session)
    assert not feature.closed and not session.closed
    release.set()
    assert update_task is not None
    await update_task
    await app.aclose()
    await session.close()
    assert history == ["feature:started", "poller:stopped", "feature:closed", "session:closed"]

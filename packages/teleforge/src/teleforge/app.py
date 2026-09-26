"""Application composition on native aiogram routers and dispatcher lifecycle."""

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any, Literal, Self, cast
from weakref import WeakKeyDictionary

from aiogram import Bot, Dispatcher, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import EVENT_CONTEXT_KEY, EventContext
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.middleware import FSMContextMiddleware
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.strategy import FSMStrategy
from aiogram.methods import TelegramMethod
from aiogram.types import InaccessibleMessage, TelegramObject, Update

from .binding import adapter_for
from .cards import _CardLocks
from .declarations import NativeFilter
from .feature import CompilationError, CompiledHandler, Diagnostic, Feature, compile_feature
from .inputs import InputError
from .isolation import IsolationScope, ScopedStorage
from .outcome import Invocation, InvocationMiddleware

type ResourceFactory = Callable[[], AbstractAsyncContextManager[object]]
type NextHandler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class AdmissionClosed(RuntimeError):
    """The standalone application has stopped accepting Telegram updates."""


class DrainTimeout(RuntimeError):
    """Updates did not release application resources after cancellation."""

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining
        super().__init__(f"{remaining} update task(s) still own application resources")


class _Dispatcher(Dispatcher):
    def __init__(self, app: App, **kwargs: Any) -> None:
        self._app = app
        self._polling_phase: Literal["startup", "polling", "cleanup"] = "startup"
        self._polling_stop_requested = False
        super().__init__(**kwargs)

    def _reset_polling(self) -> None:
        self._polling_phase = "startup"
        self._polling_stop_requested = False

    async def emit_startup(self, *args: Any, **kwargs: Any) -> None:
        await super().emit_startup(*args, **kwargs)
        if self._polling_stop_requested:
            # A startup callback may have suppressed its cancellation. It must
            # not start a fresh poller after the owner requested shutdown.
            raise asyncio.CancelledError
        # Native start_polling creates its tasks immediately after this returns,
        # with no intervening await. Before this point cancellation is safe.
        self._polling_phase = "polling"

    async def feed_update(self, bot: Bot, update: Update, **kwargs: Any) -> Any:
        # Enclose the native dispatcher, including its outer error middleware.
        async with self._app._admit_update():
            result = await super().feed_update(bot, update, **kwargs)
            # Standalone owns delivery. Returning a method to native polling or
            # webhook response handling would let the send outlive admission.
            return await bot(result) if isinstance(result, TelegramMethod) else result


class _Workflow(BaseMiddleware):
    def __init__(self, app: App) -> None:
        self.app = app

    async def __call__(self, handler: NextHandler, event: TelegramObject, data: dict[str, Any]) -> Any:
        # Native filter/middleware values take precedence over application defaults.
        for name, value in self.app.data.items():
            data.setdefault(name, value)
        data["_teleforge_media_slots"] = self.app._media_slots
        data["_teleforge_card_locks"] = self.app._card_locks
        data["_teleforge_input_formatter"] = self.app.input_formatter
        data.setdefault("teleforge_invocation", Invocation())
        return await handler(event, data)


class _ScopedFSM(FSMContextMiddleware):
    async def __call__(self, handler: NextHandler, event: TelegramObject, data: dict[str, Any]) -> Any:
        data["_teleforge_isolation"] = None
        context = data.get(EVENT_CONTEXT_KEY)
        inaccessible = (
            isinstance(event, Update)
            and event.callback_query is not None
            and isinstance(event.callback_query.message, InaccessibleMessage)
        )
        if inaccessible or not isinstance(context, EventContext) or context.chat is None:
            # Native FSM otherwise invents a private chat for chatless events; an
            # inaccessible callback also cannot establish its real topic identity.
            data["fsm_storage"] = self.storage
            return await handler(event, data)
        bot = cast(Bot, data["bot"])
        state = self.resolve_event_context(bot, data)
        data["fsm_storage"] = self.storage
        if state is None:
            return await handler(event, data)
        # Acquire before loading state and selecting a native handler. The
        # selected handler may explicitly finish this lease before slow work.
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(self.events_isolation.lock(key=state.key))
            scope = IsolationScope(stack)
            guarded = ScopedStorage(self.storage, scope)
            state = FSMContext(guarded, state.key)
            data.update(
                state=state,
                raw_state=await state.get_state(),
                fsm_storage=guarded,
                _teleforge_isolation=scope,
            )
            try:
                return await handler(event, data)
            finally:
                await scope.release()


class App:
    def __init__(
        self,
        *features: Feature,
        bot: Bot | None = None,
        data: Mapping[str, Any] | None = None,
        media_concurrency: int = 2,
        drain_timeout: float = 30,
        cancel_timeout: float = 5,
        input_formatter: Callable[[InputError], str] = str,
    ) -> None:
        if type(media_concurrency) is not int or media_concurrency < 1:
            raise ValueError("media_concurrency must be a positive integer")
        if not all(math.isfinite(value) and value >= 0 for value in (drain_timeout, cancel_timeout)):
            raise ValueError("Update drain timeouts must be finite and nonnegative")
        self.bot = bot
        self.data = dict(data or {})
        self.input_formatter = input_formatter
        self._features: list[Feature] = list(features)
        self._media_slots = asyncio.Semaphore(media_concurrency)
        self._card_locks = _CardLocks()
        self._resources: list[ResourceFactory] = []
        self._stack: AsyncExitStack | None = None
        self._configuration_closed = False
        self._lifecycle_lock = asyncio.Lock()
        self._dispatcher: Dispatcher | None = None
        self._fsm_closed = False
        self._accepting = True
        self._updates: dict[asyncio.Task[Any], int] = {}
        self._updates_done = asyncio.Event()
        self._updates_done.set()
        self._drain_timeout, self._cancel_timeout = drain_timeout, cancel_timeout
        self._polling_owned = False
        self._registrations: WeakKeyDictionary[Router, set[str]] = WeakKeyDictionary()

    @asynccontextmanager
    async def _admit_update(self) -> AsyncIterator[None]:
        if not self._accepting:
            raise AdmissionClosed("The application is shutting down")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Update handling requires an asyncio task")
        self._updates[task] = self._updates.get(task, 0) + 1
        self._updates_done.clear()
        try:
            yield
        finally:
            depth = self._updates[task] - 1
            if depth:
                self._updates[task] = depth
            else:
                del self._updates[task]
            if not self._updates:
                self._updates_done.set()

    async def _drain_updates(self) -> None:
        if not self._updates:
            return
        try:
            async with asyncio.timeout(self._drain_timeout):
                await self._updates_done.wait()
        except TimeoutError:
            for task in tuple(self._updates):
                task.cancel()
            try:
                async with asyncio.timeout(self._cancel_timeout):
                    await self._updates_done.wait()
            except TimeoutError:
                raise DrainTimeout(len(self._updates)) from None

    @property
    def features(self) -> tuple[Feature, ...]:
        return tuple(self._features)

    def include(self, feature: Feature) -> App:
        if self._configuration_closed or self._dispatcher is not None:
            raise RuntimeError("Include features before building the standalone dispatcher or starting the application")
        self._features.append(feature)
        return self

    def resource(self, factory: ResourceFactory) -> App:
        """Own a context-manager factory; ordinary closures wire its dependencies."""
        if self._configuration_closed:
            raise RuntimeError("Register resources before application startup")
        self._resources.append(factory)
        return self

    def _compile(self) -> tuple[tuple[CompiledHandler, ...], tuple[Diagnostic, ...]]:
        handlers: list[CompiledHandler] = []
        errors: list[Diagnostic] = []
        seen: set[str] = set()
        for feature in self.features:
            if feature.key in seen:
                errors.append(
                    Diagnostic("duplicate-feature", "Feature keys must be unique within an application", feature.key)
                )
            seen.add(feature.key)
            compiled, diagnostics = compile_feature(feature)
            handlers.extend(compiled)
            errors.extend(diagnostics)
        return tuple(handlers), tuple(errors)

    def check(self) -> tuple[Diagnostic, ...]:
        return self._compile()[1]

    def inspect(self) -> dict[str, object]:
        handlers, errors = self._compile()
        return {
            "schema_version": 1,
            "features": [{"key": feature.key, "type": type(feature).__qualname__} for feature in self.features],
            "handlers": [handler.as_dict() for handler in handlers],
            "diagnostics": [error.as_dict() for error in errors],
            "dependencies": sorted(self.data),
            "resources": len(self._resources),
        }

    def iter_handlers(self, kind: str | None = None) -> tuple[CompiledHandler, ...]:
        handlers, errors = self._compile()
        if errors:
            raise CompilationError(errors)
        return tuple(item for item in handlers if kind is None or item.declaration.kind == kind)

    def build_router(self) -> Router:
        """Build a fresh native router for embedding; the host owns its lifecycle/FSM."""
        handlers = self.iter_handlers()
        root = Router(name="teleforge")
        self._register(root, ())
        for feature in self.features:
            router = Router(name=feature.key)
            self._register(
                router,
                tuple(item for item in handlers if item.feature is feature and item.declaration.event is not None),
            )
            root.include_router(router)
        return root

    def register(self, router: Router, *methods: Callable[..., Any]) -> None:
        """Place declared methods among native routes without repeating their filters or flags."""
        handlers = self.iter_handlers()
        selected: list[CompiledHandler] = []
        for method in methods:
            matches = [
                item
                for item in handlers
                if getattr(item.handler, "__self__", None) is getattr(method, "__self__", None)
                and getattr(item.handler, "__func__", item.handler) is getattr(method, "__func__", method)
                and item.declaration.event is not None
            ]
            if not matches:
                raise ValueError("Register a declared Telegram method from a feature included in this app")
            selected.extend(matches)
        self._register(router, tuple(selected))

    def _register(self, router: Router, handlers: tuple[CompiledHandler, ...]) -> None:
        if any(
            isinstance(middleware, _Workflow) and middleware.app is not self
            for observer in router.observers.values()
            for middleware in observer.outer_middleware
        ):
            raise ValueError("A native router can belong to only one App")
        existing = self._registrations.get(router, set())
        identities = [item.key for item in handlers]
        if existing.intersection(identities) or len(identities) != len(set(identities)):
            raise ValueError("A declared handler is already registered on this router")
        if router not in self._registrations:
            for observer in router.observers.values():
                observer.outer_middleware(_Workflow(self))
            self._registrations[router] = existing
        for compiled in handlers:
            declaration = compiled.declaration
            assert declaration.event is not None
            filters = list(declaration.filters)
            if declaration.filter_factory is not None:
                filters.extend(declaration.filter_factory(compiled.feature))
            if declaration.kind == "command":
                if not any(isinstance(item, StateFilter) for item in filters):
                    filters.insert(0, StateFilter(None))
                custom_filter = declaration.metadata.get("_command_filter")
                filters.append(
                    cast(NativeFilter, custom_filter)
                    if custom_filter is not None
                    else Command(*declaration.names, ignore_case=True)
                )
            flags = dict(declaration.flags)
            flags.setdefault("handler_key", compiled.key)
            flags.setdefault("feature_key", compiled.feature.key)
            router.observers[declaration.event].register(adapter_for(compiled), *filters, flags=flags)
            existing.add(compiled.key)

    def create_dispatcher(
        self,
        *,
        storage: BaseStorage | None = None,
        events_isolation: BaseEventIsolation | None = None,
        fsm_strategy: FSMStrategy = FSMStrategy.USER_IN_TOPIC,
    ) -> Dispatcher:
        if not self._accepting:
            raise AdmissionClosed("A closed application cannot create a dispatcher")
        if self._dispatcher is not None:
            raise RuntimeError("This application already has a standalone dispatcher; use build_router for embedding")
        dispatcher = _Dispatcher(
            self,
            storage=storage if storage is not None else MemoryStorage(),
            events_isolation=events_isolation if events_isolation is not None else SimpleEventIsolation(),
            fsm_strategy=fsm_strategy,
            disable_fsm=True,
            **self.data,
        )
        dispatcher.fsm = _ScopedFSM(dispatcher.fsm.storage, dispatcher.fsm.events_isolation, fsm_strategy)
        dispatcher.update.outer_middleware(InvocationMiddleware())
        dispatcher.update.outer_middleware(dispatcher.fsm)
        # A fresh Dispatcher has only fsm.close here. Workers/resources must stop
        # before storage closes; this app owns that single shutdown boundary.
        dispatcher.shutdown.handlers.clear()
        dispatcher.startup.register(self.start)
        dispatcher.shutdown.register(self._dispatcher_shutdown)
        dispatcher.include_router(self.build_router())
        self._dispatcher = dispatcher
        self._fsm_closed = False
        return dispatcher

    async def _dispatcher_shutdown(self) -> None:
        # run_polling closes resources after joining the complete native runner,
        # preserving its primary error if application cleanup also fails.
        if not self._polling_owned:
            await self.aclose()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if not self._accepting:
                raise AdmissionClosed("A closed application cannot restart")
            if self._stack is not None:
                return
            if self._fsm_closed:
                raise RuntimeError("A closed standalone application cannot restart its storage")
            errors = self.check()
            if errors:
                raise CompilationError(errors)
            self._configuration_closed = True
            stack = AsyncExitStack()
            try:
                for factory in self._resources:
                    await stack.enter_async_context(factory())
                for feature in self.features:
                    await stack.enter_async_context(feature.lifespan(self))
            except BaseException as primary:
                if self._polling_owned and isinstance(self._dispatcher, _Dispatcher):
                    # Failed startup already owns an unwind. Owner cancellation
                    # must join it rather than interrupting earlier resources.
                    self._dispatcher._polling_phase = "cleanup"
                try:
                    await stack.aclose()
                except BaseException as cleanup:
                    primary.add_note(f"Startup cleanup also failed ({type(cleanup).__name__}).")
                    raise primary from cleanup
                raise
            self._stack = stack

    async def aclose(self) -> None:
        if asyncio.current_task() in self._updates:
            raise RuntimeError("An update cannot close its own application; request shutdown from the owner")
        async with self._lifecycle_lock:
            self._configuration_closed = True
            self._accepting = False
            # Leave clients open if a running update cannot be joined. The
            # application owner can terminate the process or retry shutdown.
            await self._drain_updates()
            stack, self._stack = self._stack, None
            try:
                if stack is not None:
                    await stack.aclose()
            finally:
                if self._dispatcher is not None and not self._fsm_closed:
                    self._fsm_closed = True
                    await self._dispatcher.fsm.close()

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[App]:
        await self.start()
        try:
            yield self
        finally:
            await self.aclose()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def feed_update(self, bot: Bot, update: Update, **data: Any) -> object:
        dispatcher = self._dispatcher or self.create_dispatcher()
        await self.start()
        return await dispatcher.feed_update(bot, update, **data)

    async def run_polling(self, bot: Bot | None = None, *, close_bot_session: bool = False, **options: Any) -> None:
        selected = bot if bot is not None else self.bot
        if selected is None:
            raise TypeError("Supply a Bot to App or run_polling")
        if self._polling_owned:
            raise RuntimeError("This application already owns a polling runner")
        dispatcher = self._dispatcher or self.create_dispatcher()
        assert isinstance(dispatcher, _Dispatcher)
        dispatcher._reset_polling()
        self._polling_owned = True
        runner = asyncio.create_task(
            self._polling_lifetime(dispatcher, selected, close_bot_session, options), name="teleforge.polling"
        )
        stopper: asyncio.Task[None] | None = None
        primary: BaseException | None = None

        async def stop() -> None:
            if dispatcher._polling_phase == "polling":
                await dispatcher.stop_polling()

        while not runner.done():
            try:
                # Native runner and resource teardown share one task/context.
                # Join without forwarding owner cancellation; retrieve the
                # outcome once below, including after repeated cancellation.
                await asyncio.wait((runner,))
            except asyncio.CancelledError as error:
                if primary is None:
                    primary = error
                if not runner.done() and not dispatcher._polling_stop_requested:
                    dispatcher._polling_stop_requested = True
                    if dispatcher._polling_phase == "polling":
                        stopper = asyncio.create_task(stop(), name="teleforge.polling.stop")
                    elif dispatcher._polling_phase == "startup":
                        runner.cancel()
        failures: list[BaseException] = []
        try:
            runner.result()
        except BaseException as error:  # noqa: BLE001 - preserve the original native/lifecycle error
            failures.append(error)
        if stopper is not None:
            # A failing native shutdown does not set aiogram's stopped event.
            # Our runner is joined, so retire the exact stop waiter we own.
            if not stopper.done():
                stopper.cancel()
            while not stopper.done():
                try:
                    await asyncio.wait((stopper,))
                except asyncio.CancelledError as error:
                    if primary is None:
                        primary = error
            try:
                stopper.result()
            except asyncio.CancelledError:
                pass
            except BaseException as error:  # noqa: BLE001 - retain a secondary stop failure
                failures.append(error)
        self._polling_owned = False
        if primary is None and failures:
            primary = failures[0]
        if primary is not None:
            secondary = next(
                (error for error in failures if error is not primary and not isinstance(error, asyncio.CancelledError)),
                None,
            )
            if secondary is not None:
                primary.add_note(f"Polling cleanup also failed ({type(secondary).__name__}).")
                raise primary from secondary
            raise primary

    async def _polling_lifetime(
        self, dispatcher: _Dispatcher, bot: Bot, close_bot_session: bool, options: dict[str, Any]
    ) -> None:
        primary: BaseException | None = None
        try:
            await dispatcher.start_polling(bot, close_bot_session=False, **options)
        except BaseException as error:  # noqa: BLE001 - cleanup must preserve native errors and startup cancellation
            primary = error
        dispatcher._polling_phase = "cleanup"
        try:
            await self.aclose()
            if close_bot_session:
                await bot.session.close()
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"Polling cleanup also failed ({type(cleanup).__name__}).")
            raise primary from cleanup
        if primary is not None:
            raise primary

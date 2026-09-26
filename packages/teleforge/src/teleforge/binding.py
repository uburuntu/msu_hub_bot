"""One native event-first adapter owns preparation, invocation and delivery."""

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from typing import Any, cast

from aiogram import Bot
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters.command import CommandObject
from aiogram.methods import TelegramMethod
from aiogram.types import TelegramObject
from aiogram.utils.formatting import Text
from pydantic import BaseModel

from .context import CallbackContext, Context, context_for
from .feature import CompiledHandler
from .inputs import Declaration as InputDeclaration
from .inputs import InputError, prepare_arguments
from .isolation import IsolationError
from .outcome import Invocation, attach_outcome

type Adapter = Callable[..., Awaitable[object]]


class _AcquisitionRejected(Exception):
    def __init__(self, issue: InputError) -> None:
        self.issue = issue
        super().__init__(issue.code)


async def _deliver(ctx: Context, value: object, compiled: CompiledHandler) -> object:
    if value is None or isinstance(value, bool | TelegramObject):
        return value
    if isinstance(value, list) and all(isinstance(item, bool | TelegramObject) for item in value):
        return value
    if isinstance(value, TelegramMethod):
        # Native method returns must execute here, while telemetry/input scopes are open.
        return await ctx._execute_native(value)
    if isinstance(ctx, CallbackContext):
        raise TypeError("Callback output must use ctx.answer, ctx.edit or ctx.reply explicitly")
    if isinstance(value, str | Text):
        return await ctx.reply(value)
    raise TypeError(f"{compiled.key} returned unsupported output; use ctx.reply(photo=..., document=...) explicitly")


async def invoke_handler(compiled: CompiledHandler, event: TelegramObject, **data: Any) -> object:
    bot = data.get("bot")
    if not isinstance(bot, Bot):
        bot = event.bot
    if bot is None:
        raise TypeError("An invocation requires an aiogram Bot")
    ctx = context_for(bot, event, data=data, policy=compiled.declaration.policy)
    data["_teleforge_context"] = ctx
    invocation = data.setdefault("teleforge_invocation", Invocation())
    if not isinstance(invocation, Invocation):
        raise TypeError("teleforge_invocation is reserved for the shared invocation holder")
    invocation.context = ctx
    if isinstance(ctx, CallbackContext) and compiled.declaration.ack == "manual":
        ctx.manual_ack()
    try:
        return await _invoke(compiled, event, ctx, data)
    except SkipHandler as error:
        if ctx._isolation_released:
            failure = IsolationError("A released terminal handler cannot skip to another route")
            attach_outcome(failure, ctx.outcome)
            raise failure from None
        attach_outcome(error, ctx.outcome)
        raise
    except BaseException as error:
        attach_outcome(error, ctx.outcome)
        raise


async def _invoke(compiled: CompiledHandler, event: TelegramObject, ctx: Context, data: dict[str, Any]) -> object:
    command = data.get("command")
    tail = data.get("_teleforge_tail", (command.args or "") if isinstance(command, CommandObject) else None)
    if tail is not None and not isinstance(tail, str):
        raise TypeError("A custom command filter must provide a string _teleforge_tail")
    if "_teleforge_text" in data and not isinstance(data["_teleforge_text"], str):
        raise TypeError("A custom command filter must provide a string _teleforge_text")
    called = False
    handled_result: object = object()
    async with AsyncExitStack() as resources:

        async def call() -> object:
            nonlocal called, handled_result
            if called:
                raise RuntimeError("An invocation hook cannot execute the handler more than once")
            called = True
            payload = data.get("_teleforge_payload", data.get("callback_data"))
            if payload is not None and not isinstance(payload, BaseModel | Mapping):
                raise TypeError("Callback payload must be a validated model or mapping")
            try:
                kwargs = await resources.enter_async_context(
                    prepare_arguments(
                        compiled.handler,
                        event,
                        ctx,
                        data,
                        cast(Mapping[str, InputDeclaration], compiled.declaration.inputs),
                        tail=tail,
                        payload=payload,
                        plan=compiled.plan,
                    )
                )
            except InputError as issue:
                raise _AcquisitionRejected(issue) from None
            result = await compiled.handler(**kwargs)
            ctx._handler_returned = True
            # Hook-owned scopes (notably a card's UI lock) include presentation
            # of the handler's native return value as well as the action itself.
            handled_result = await _deliver(ctx, result, compiled)
            return handled_result

        if compiled.declaration.flags.get("fsm_release") is True and "_teleforge_isolation" in data:
            await ctx.release_isolation()
        hook = compiled.declaration.hook
        try:
            result = await call() if hook is None else await hook(compiled.feature, ctx, data, call)
        except _AcquisitionRejected as rejected:
            await ctx.guide(rejected.issue)
            delivered = None
        else:
            # Hooks may supply their own output. A pass-through result was
            # already normalized, including native methods returning strings.
            delivered = result if result is handled_result else await _deliver(ctx, result, compiled)
        await ctx.finish()
        return delivered


def adapter_for(compiled: CompiledHandler) -> Adapter:
    async def adapter(event: TelegramObject, **data: Any) -> object:
        return await invoke_handler(compiled, event, **data)

    # aiogram unwraps functions before inspecting them. Deliberately no __wrapped__.
    adapter.__name__ = compiled.handler.__name__
    adapter.__qualname__ = compiled.handler.__qualname__
    adapter.__module__ = compiled.handler.__module__
    adapter.__doc__ = compiled.handler.__doc__
    return adapter

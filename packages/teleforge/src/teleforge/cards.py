"""Managed cards bind typed feature methods without owning application state."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypedDict, TypeVar, cast, get_type_hints

from aiogram.filters import Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity
from aiogram.utils.formatting import Text
from pydantic import TypeAdapter, ValidationError

from .context import CallbackContext, Context
from .declarations import Declaration, attach_declaration, declarations_of
from .delivery import MediaSource
from .feature import Feature
from .inputs import InputError, _ValidatedPayload
from .parameters import ParameterPlan, checked_dependency, compile_parameters

if TYPE_CHECKING:
    from .outcome import InvocationOutcome

_Handler = TypeVar("_Handler", bound=Callable[..., Any])
_PREFIX = "tf:"


class CardError(ValueError):
    """Invalid card declaration, button arguments, or Telegram target."""


class CardRefreshError(RuntimeError):
    """The handler returned, but refreshing its presentation failed. Do not replay it."""

    handler_returned = True

    def __init__(self, renderer: str, *, outcome: InvocationOutcome) -> None:
        self.teleforge_outcome = outcome
        super().__init__(f"Handler returned but card {renderer!r} could not be refreshed")


class CardContent(TypedDict, total=False):
    """Native delivery arguments; media resources remain owned by the caller."""

    text: str | Text | None
    reply_markup: InlineKeyboardMarkup
    photo: MediaSource | None
    video: MediaSource | None
    animation: MediaSource | None
    audio: MediaSource | None
    document: MediaSource | None
    entities: Sequence[MessageEntity] | None


@dataclass(frozen=True, init=False)
class Button:
    """A label and a bound, declared feature action with small typed arguments."""

    text: str
    action: Callable[..., Any]
    arguments: Mapping[str, object]

    def __init__(self, text: str, action: Callable[..., Any], **arguments: object) -> None:
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "arguments", dict(arguments))


@dataclass(frozen=True)
class Card:
    """One editable message and a freshly rendered keyboard; rendering has no effects."""

    text: str | Text | None = None
    buttons: Sequence[Sequence[Button | InlineKeyboardButton]] = ()
    photo: MediaSource | None = None
    video: MediaSource | None = None
    animation: MediaSource | None = None
    audio: MediaSource | None = None
    document: MediaSource | None = None
    entities: Sequence[MessageEntity] | None = None

    def _content(self, keyboard: InlineKeyboardMarkup) -> CardContent:
        return {
            "text": self.text,
            "reply_markup": keyboard,
            "photo": self.photo,
            "video": self.video,
            "animation": self.animation,
            "audio": self.audio,
            "document": self.document,
            "entities": self.entities,
        }


def _bound(method: Callable[..., Any]) -> tuple[Feature, str]:
    owner = getattr(method, "__self__", None)
    if not isinstance(owner, Feature):
        raise CardError("Card renderers and actions must be bound feature methods")
    return owner, method.__name__


def _declaration(method: Callable[..., Any], marker: str) -> Declaration:
    _, name = _bound(method)
    found = [item for item in declarations_of(method) if marker in item.metadata]
    if len(found) != 1:
        raise CardError(f"Method {name!r} must have exactly one {marker} declaration")
    return found[0]


@dataclass(frozen=True)
class _Parameter:
    name: str
    adapter: TypeAdapter[Any]
    annotation: object
    default: object = inspect.Parameter.empty


def _plan(method: Callable[..., Any], declaration: Declaration) -> ParameterPlan:
    feature = getattr(method, "__self__", None)
    namespace: dict[str, Any] = {}
    if isinstance(feature, Feature):
        for cls in reversed(type(feature).__mro__):
            namespace.update(vars(cls))
            namespace[cls.__name__] = cls
    try:
        hints = get_type_hints(method, localns=namespace, include_extras=True)
        plan, issues = compile_parameters(method, declaration, annotations=hints)
    except (NameError, TypeError) as exc:
        raise CardError(f"Cannot resolve card method annotations ({type(exc).__name__})") from exc
    if issues:
        raise CardError("; ".join(issue.message for issue in issues))
    return plan


def _parameters(method: Callable[..., Any], declaration: Declaration) -> list[_Parameter]:
    parameters = []
    for parameter in _plan(method, declaration).parameters:
        if parameter.source not in {"native", "callback_payload"}:
            continue
        try:
            adapter: TypeAdapter[Any] = TypeAdapter(parameter.annotation)
            adapter.json_schema()
        except (ValueError, TypeError) as exc:
            raise CardError(f"Card argument {parameter.name!r} needs a JSON-compatible type") from exc
        parameters.append(_Parameter(parameter.name, adapter, parameter.annotation, parameter.default))
    return parameters


def _schema(action_method: Callable[..., Any]) -> tuple[str, list[_Parameter], Callable[..., Any]]:
    feature, _ = _bound(action_method)
    declaration = _declaration(action_method, "card_action")
    renderer_name = cast(str, declaration.metadata["card_action"])
    renderer = getattr(feature, renderer_name, None)
    if not callable(renderer):
        raise CardError(f"Card renderer {renderer_name!r} must be a declared feature method")
    renderer_declaration = _declaration(renderer, "card")
    parameters = _parameters(action_method, declaration)
    by_name = {parameter.name: parameter for parameter in parameters}
    for parameter in _parameters(renderer, renderer_declaration):
        previous = by_name.get(parameter.name)
        if previous is not None and previous.annotation != parameter.annotation:
            raise CardError(f"Card and action disagree on argument {parameter.name!r}")
        if previous is None:
            parameters.append(parameter)
            by_name[parameter.name] = parameter
    if declaration.payload is not None:
        fields = declaration.payload.model_fields
        for parameter in parameters:
            if parameter.name not in fields or parameter.annotation != fields[parameter.name].annotation:
                raise CardError(f"Card argument {parameter.name!r} must match its native CallbackData field")
        # Native CallbackData owns the wire identity, defaults and validators.
        return "", parameters, renderer
    # Annotation repr can contain function addresses (Annotated validators), so
    # buttons would stop matching after every process restart. The public input
    # schema is stable; validators still run on each decoded callback.
    identity = json.dumps(
        [feature.key, declaration.metadata["card_action_key"], [(p.name, p.adapter.json_schema()) for p in parameters]],
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.blake2s(identity.encode(), digest_size=6).digest()
    return base64.urlsafe_b64encode(digest).decode(), parameters, renderer


def validate_card(method: Callable[..., Any]) -> None:
    """Validate the same static contract used by packing, filtering and rendering."""
    for declaration in declarations_of(method):
        if "card_action" in declaration.metadata:
            _schema(method)
        elif "card" in declaration.metadata:
            _parameters(method, declaration)


def _validate(
    parameters: Sequence[_Parameter], arguments: Mapping[str, object], *, json_values: bool = False
) -> dict[str, Any]:
    unknown = arguments.keys() - {parameter.name for parameter in parameters}
    if unknown:
        raise CardError(f"Unknown card arguments: {', '.join(sorted(unknown))}")
    result = {}
    for parameter in parameters:
        value = arguments.get(parameter.name, parameter.default)
        if value is inspect.Parameter.empty:
            raise CardError(f"Missing card argument {parameter.name!r}")
        try:
            encoded_before = parameter.adapter.dump_python(value, mode="json", warnings=False)
            result[parameter.name] = (
                parameter.adapter.validate_json(json.dumps(value, allow_nan=False), strict=True)
                if json_values
                else parameter.adapter.validate_python(value, strict=True)
            )
            parsed = result[parameter.name]
            # Pydantic's Literal validator uses equality even in strict mode:
            # true can otherwise select Literal[1], and 1 can select Literal[True].
            if isinstance(value, bool) != isinstance(parsed, bool) or (type(value) is float and type(parsed) is int):
                raise CardError(f"Invalid card argument {parameter.name!r}")
            before = json.dumps(encoded_before, sort_keys=True, separators=(",", ":"), allow_nan=False)
            after = json.dumps(
                parameter.adapter.dump_python(parsed, mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if after != before:
                raise CardError(
                    f"Card argument {parameter.name!r} must preserve its encoded value; normalize before building a Button"
                )
        except CardError:
            raise
        except (ValueError, TypeError, ValidationError) as exc:
            raise CardError(f"Invalid card argument {parameter.name!r}") from exc
    return result


def _pack(method: Callable[..., Any], arguments: Mapping[str, object]) -> str:
    identity, parameters, _ = _schema(method)
    payload = _declaration(method, "card_action").payload
    if payload is not None:
        if unknown := arguments.keys() - payload.model_fields.keys():
            raise CardError(f"Unknown card arguments: {', '.join(sorted(unknown))}")
        try:
            return payload.model_validate(arguments).pack()
        except (ValueError, TypeError) as exc:
            raise CardError("Invalid native card callback arguments") from exc
    values = _validate(parameters, arguments)
    raw = [parameter.adapter.dump_python(values[parameter.name], mode="json") for parameter in parameters]
    encoded = _PREFIX + identity + ":" + json.dumps(raw, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 64:
        raise CardError("Card button exceeds Telegram's 64-byte limit; pass a short application record ID")
    return encoded


class _ActionFilter(Filter):
    def __init__(self, method: Callable[..., Any]) -> None:
        self.identity, self.parameters, _ = _schema(method)
        payload = _declaration(method, "card_action").payload
        self.native = payload.filter() if payload is not None else None

    async def __call__(self, query: CallbackQuery) -> bool | dict[str, Any]:
        if not query.data or len(query.data.encode()) > 64:
            return False
        if self.native is not None:
            matched = await self.native(query)
            if not matched:
                return False
            payload = matched["callback_data"]
            return {
                **matched,
                "_teleforge_payload": payload,
                "_teleforge_card_arguments": _ValidatedPayload(payload.model_dump(mode="python")),
            }
        if not query.data.startswith(_PREFIX + self.identity + ":"):
            return False
        try:
            values = json.loads(query.data[len(_PREFIX) + len(self.identity) + 1 :])
            if not isinstance(values, list) or len(values) != len(self.parameters):
                return False
            arguments = _validate(
                self.parameters, dict(zip((p.name for p in self.parameters), values, strict=True)), json_values=True
            )
        except ValueError, TypeError:
            return False
        validated = _ValidatedPayload(arguments)
        return {"_teleforge_payload": validated, "_teleforge_card_arguments": validated}


@dataclass
class _Lock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class _CardLocks:
    def __init__(self) -> None:
        self._locks: dict[tuple[int, str | None, int, int], _Lock] = {}

    @asynccontextmanager
    async def hold(self, key: tuple[int, str | None, int, int], *, coalesce: bool = False) -> AsyncIterator[bool]:
        entry = self._locks.setdefault(key, _Lock())
        entry.users += 1
        try:
            if coalesce and entry.users > 1:
                yield False
                return
            async with entry.lock:
                yield True
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._locks[key]


def _locks_for(ctx: Context, feature: Feature) -> _CardLocks:
    registry = ctx.data.get("_teleforge_card_locks")
    if registry is None:
        # Direct invocation without App has only this feature's lifetime. App
        # injects its shared registry for every routed feature and renderer.
        registry = vars(feature).setdefault("_teleforge_card_locks", _CardLocks())
    if not isinstance(registry, _CardLocks):
        raise CardError("Managed actions require the application's card lock registry")
    return registry


def _keyboard(view: Card, renderer: Callable[..., Any] | None, arguments: Mapping[str, object]) -> InlineKeyboardMarkup:
    owner = getattr(renderer, "__self__", None)
    renderer_name = getattr(renderer, "__name__", None)
    rows = []
    for row in view.buttons:
        buttons = []
        for button in row:
            if isinstance(button, InlineKeyboardButton):
                buttons.append(button)
                continue
            action_owner, _ = _bound(button.action)
            declaration = _declaration(button.action, "card_action")
            validate_card(button.action)
            if isinstance(owner, Feature) and (
                action_owner is not owner or declaration.metadata["card_action"] != renderer_name
            ):
                raise CardError("A managed button must target an action of the card's feature and renderer")
            buttons.append(
                InlineKeyboardButton(
                    text=button.text, callback_data=_pack(button.action, {**arguments, **button.arguments})
                )
            )
        rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(
    ctx: Context | None,
    renderer: Callable[..., Any],
    arguments: Mapping[str, object],
    *,
    data: Mapping[str, Any] | None = None,
    action_arguments: bool = False,
) -> tuple[Card, dict[str, Any]]:
    declaration = Declaration(kind="card", metadata={"card": True})
    parameters = _parameters(renderer, declaration)
    selected = {p.name: arguments[p.name] for p in parameters if p.name in arguments} if action_arguments else arguments
    values = dict(selected) if isinstance(arguments, _ValidatedPayload) else _validate(parameters, selected)
    injected: dict[str, Any] = {}
    dependencies = data if data is not None else ctx.data if ctx is not None else {}
    for parameter in _plan(renderer, declaration).parameters:
        if parameter.source == "context":
            if ctx is None:
                raise CardError(f"Renderer parameter {parameter.name!r} requires an explicit invocation context")
            injected[parameter.name] = checked_dependency(parameter.annotation, ctx, parameter.name)
        elif parameter.source == "dependency":
            value = dependencies.get(parameter.name, parameter.default)
            if value is inspect.Parameter.empty:
                raise CardError(f"Missing renderer dependency {parameter.name!r}; supply data explicitly")
            injected[parameter.name] = checked_dependency(parameter.annotation, value, parameter.name)
    result = renderer(**injected, **values)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Card):
        raise CardError("A card renderer must return Card")
    return result, values


async def prepare_card(
    renderer: Callable[..., Any] | Card,
    *,
    data: Mapping[str, Any] | None = None,
    context: Context | None = None,
    **arguments: object,
) -> CardContent:
    """Prepare native arguments for send_response/edit_response without sending.

    A plain renderer needs no event. Keyword-only dependencies come from data
    (or an explicitly supplied context). For an already-rendered Card, managed
    buttons carry their full payload. Keep caller-owned media resources open
    until the subsequent send or edit finishes; preparation does not copy them.
    """
    if isinstance(renderer, Card):
        if arguments:
            raise CardError("An already-rendered Card takes its payload from each Button")
        return renderer._content(_keyboard(renderer, None, {}))
    view, values = await _render(context, renderer, arguments, data=data)
    return view._content(_keyboard(view, renderer, values))


async def show(ctx: Context, renderer: Callable[..., Any], **arguments: object) -> Message:
    """Render and send one card. The application binds durable UI identity if needed."""
    content = await prepare_card(renderer, data=ctx.data, context=ctx, **arguments)
    return await ctx.reply(**content, fixed=True)


def card[H: Callable[..., Any]](method: H) -> H:
    """Mark a pure feature renderer; direct method calls remain ordinary Python."""
    attach_declaration(method, Declaration(kind="card", metadata={"card": True}))
    return method


def action(
    *,
    key: str,
    card: str,
    payload: type[CallbackData] | None = None,
    refresh: bool = True,
    ack: Literal["auto", "early"] = "auto",
    coalesce: bool = False,
    flags: Mapping[str, Any] | None = None,
    filters: tuple[Callable[..., Any], ...] = (),
) -> Callable[[_Handler], _Handler]:
    """Declare an action; application code guards actor, origin and revision.

    Early acknowledgement happens before the card lock and forfeits later alert
    results. Coalescing drops clicks while the same UI is busy: opt in only for
    disposable refresh requests, never for mutations that each need to run.
    A native CallbackData payload preserves its existing wire format and model
    validation while using the same lock, acknowledgement and refresh pipeline.
    """
    if ack not in {"auto", "early"}:
        raise CardError("Managed action acknowledgement must be 'auto' or 'early'")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}", key):
        raise CardError("Managed actions need a stable 1–64 character key")

    def decorate(method: _Handler) -> _Handler:
        name = method.__name__

        def bound_filters(feature: Feature) -> tuple[Callable[..., Any], ...]:
            return (_ActionFilter(getattr(feature, name)),)

        async def hook(
            feature: Feature, ctx: Context, data: dict[str, Any], invoke: Callable[[], Awaitable[object]]
        ) -> object:
            if not isinstance(ctx, CallbackContext) or not isinstance(ctx.event, CallbackQuery):
                raise CardError("Managed actions require a callback context")
            message = ctx.event.message
            if not isinstance(message, Message) or message.from_user is None or message.from_user.id != ctx.bot.id:
                await ctx.guide(InputError("callback-invalid"))
                return None
            arguments = cast(dict[str, object], data["_teleforge_card_arguments"])
            renderer = cast(Callable[..., Any], getattr(feature, card))
            _declaration(renderer, "card")
            locks = _locks_for(ctx, feature)
            if ack == "early" or coalesce:
                await ctx.release_isolation()
            if ack == "early":
                await ctx.answer()
            target = (ctx.bot.id, message.business_connection_id, message.chat.id, message.message_id)
            async with locks.hold(target, coalesce=coalesce) as acquired:
                if not acquired:
                    return None
                result = await invoke()
                if refresh and result is None:
                    try:
                        view, values = await _render(ctx, renderer, arguments, action_arguments=True)
                        await ctx.edit(**view._content(_keyboard(view, renderer, values)))
                    except Exception as exc:
                        raise CardRefreshError(card, outcome=ctx.outcome) from exc
                return result

        attach_declaration(
            method,
            Declaration(
                kind="callback",
                event="callback_query",
                payload=payload,
                flags=flags or {},
                filters=filters,
                filter_factory=bound_filters,
                hook=hook,
                metadata={
                    "card_action": card,
                    "card_action_key": key,
                    "refresh": refresh,
                    "ack_timing": ack,
                    "coalesce": coalesce,
                },
            ),
        )
        return method

    return decorate

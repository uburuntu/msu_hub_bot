"""Small, inert declarations attached to ordinary Python methods."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from aiogram.filters.callback_data import CallbackData, CallbackQueryFilter

from .delivery import ResponsePolicy

if TYPE_CHECKING:
    from .context import Context
    from .feature import Feature

type Handler = Callable[..., Awaitable[object]]
type NativeFilter = Callable[..., Any]
type InvocationHook = Callable[
    ["Feature", "Context", dict[str, Any], Callable[[], Awaitable[object]]], Awaitable[object]
]
type FilterFactory = Callable[["Feature"], tuple[NativeFilter, ...]]
type DeclarationKind = Literal["command", "callback", "event", "card", "job", "web"]
F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class Declaration:
    kind: DeclarationKind
    event: str | None = None
    names: tuple[str, ...] = ()
    filters: tuple[NativeFilter, ...] = ()
    inputs: Mapping[str, object] = field(default_factory=dict)
    flags: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)
    policy: ResponsePolicy = field(default_factory=ResponsePolicy)
    payload: type[CallbackData] | None = None
    ack: Literal["auto", "manual"] = "auto"
    filter_factory: FilterFactory | None = None
    hook: InvocationHook | None = None

    def __post_init__(self) -> None:
        # A decorator's caller cannot mutate registration through a borrowed dictionary.
        for name in ("inputs", "flags", "metadata"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


def attach_declaration[H: Callable[..., Any]](fn: H, declaration: Declaration) -> H:
    declarations = tuple(getattr(fn, "__teleforge_declarations__", ()))
    vars(fn)["__teleforge_declarations__"] = (declaration, *declarations)
    return fn


def declarations_of(fn: Callable[..., Any]) -> tuple[Declaration, ...]:
    """Resolve declaration inheritance without wrapping or altering the method."""
    original = getattr(fn, "__func__", fn)
    if getattr(original, "__teleforge_disabled__", False):
        return ()
    direct: tuple[Declaration, ...] = getattr(original, "__teleforge_declarations__", ())
    if direct:
        return direct
    instance = getattr(fn, "__self__", None)
    if instance is None:
        return ()
    owner = instance if isinstance(instance, type) else type(instance)
    for base in owner.__mro__:
        candidate = base.__dict__.get(original.__name__)
        if isinstance(candidate, staticmethod | classmethod):
            candidate = candidate.__func__
        if candidate is None:
            continue
        if getattr(candidate, "__teleforge_disabled__", False):
            return ()
        inherited: tuple[Declaration, ...] = getattr(candidate, "__teleforge_declarations__", ())
        if inherited:
            return inherited
    return ()


def disable[H: Callable[..., Any]](fn: H) -> H:
    """Explicitly remove an inherited entrypoint while retaining a callable method."""
    vars(fn)["__teleforge_disabled__"] = True
    return fn


def command(
    *names: str,
    filter: NativeFilter | None = None,
    filters: tuple[NativeFilter, ...] = (),
    flags: Mapping[str, Any] | None = None,
    rich: bool = True,
    soft_messages: int = 3,
    max_output_bytes: int = 20 * 1024 * 1024,
    **inputs: object,
) -> Callable[[F], F]:
    declaration = Declaration(
        kind="command",
        event="message",
        names=names,
        filters=filters,
        inputs=inputs,
        metadata={"_command_filter": filter} if filter is not None else {},
        flags=flags or {},
        policy=ResponsePolicy(rich=rich, soft_messages=soft_messages, max_output_bytes=max_output_bytes),
    )
    return lambda fn: attach_declaration(fn, declaration)


def callback(
    payload: type[CallbackData] | NativeFilter,
    *filters: NativeFilter,
    flags: Mapping[str, Any] | None = None,
    ack: Literal["auto", "manual"] = "auto",
    **inputs: object,
) -> Callable[[F], F]:
    model: type[CallbackData] | None = None
    if isinstance(payload, type) and issubclass(payload, CallbackData):
        model = payload
        native_filter: NativeFilter = payload.filter()
    else:
        native_filter = payload
        if isinstance(payload, CallbackQueryFilter):
            model = payload.callback_data
    declaration = Declaration(
        kind="callback",
        event="callback_query",
        filters=(native_filter, *filters),
        flags=flags or {},
        inputs=inputs,
        payload=model,
        ack=ack,
    )
    return lambda fn: attach_declaration(fn, declaration)


def event(
    name: str,
    *filters: NativeFilter,
    flags: Mapping[str, Any] | None = None,
    **inputs: object,
) -> Callable[[F], F]:
    declaration = Declaration(kind="event", event=name, filters=filters, flags=flags or {}, inputs=inputs)
    return lambda fn: attach_declaration(fn, declaration)


def message(
    *filters: NativeFilter,
    flags: Mapping[str, Any] | None = None,
    **inputs: object,
) -> Callable[[F], F]:
    return event("message", *filters, flags=flags, **inputs)


def edited_message(
    *filters: NativeFilter,
    flags: Mapping[str, Any] | None = None,
    **inputs: object,
) -> Callable[[F], F]:
    return event("edited_message", *filters, flags=flags, **inputs)


def inline_query(*filters: NativeFilter) -> Callable[[F], F]:
    return event("inline_query", *filters)


def chosen_inline_result(*filters: NativeFilter) -> Callable[[F], F]:
    return event("chosen_inline_result", *filters)


def pre_checkout_query(*filters: NativeFilter) -> Callable[[F], F]:
    return event("pre_checkout_query", *filters)


def shipping_query(*filters: NativeFilter) -> Callable[[F], F]:
    return event("shipping_query", *filters)

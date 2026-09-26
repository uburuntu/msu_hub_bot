"""Feature compilation is deterministic and does not open application resources."""

import inspect
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast, get_type_hints

from aiogram import Router

from .declarations import Declaration, Handler, declarations_of
from .parameters import ParameterPlan, compile_parameters

if TYPE_CHECKING:
    from .app import App


class Feature:
    """An application-scoped collection of methods and constructor dependencies."""

    _feature_key: ClassVar[str | None] = None

    def __init_subclass__(cls, *, key: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._feature_key = key

    @property
    def key(self) -> str:
        return self._feature_key or re.sub(r"(?<!^)(?=[A-Z])", "_", type(self).__name__).lower()

    @asynccontextmanager
    async def lifespan(self, app: App) -> AsyncIterator[None]:
        """Override to acquire feature resources; teardown runs in reverse order."""
        yield


@dataclass(frozen=True, slots=True)
class Source:
    file: str | None
    line: int | None

    def as_dict(self) -> dict[str, object]:
        return {"file": self.file, "line": self.line}


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    message: str
    feature: str
    handler: str | None = None
    source: Source = Source(None, None)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "feature": self.feature,
            "handler": self.handler,
            "source": self.source.as_dict(),
        }


class CompilationError(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("\n".join(f"{item.feature}.{item.handler or '*'}: {item.message}" for item in diagnostics))


@dataclass(frozen=True, slots=True)
class CompiledHandler:
    feature: Feature
    name: str
    key: str
    handler: Handler
    declaration: Declaration
    signature: inspect.Signature
    annotations: Mapping[str, Any]
    source: Source
    plan: ParameterPlan

    def as_dict(self) -> dict[str, object]:
        declaration = self.declaration
        return {
            "key": self.key,
            "method": self.name,
            "kind": declaration.kind,
            "event": declaration.event,
            "names": list(declaration.names),
            "source": self.source.as_dict(),
            "parameters": [
                {
                    "name": parameter.name,
                    "type": _type_name(parameter.annotation),
                    "has_default": parameter.default is not inspect.Parameter.empty,
                    "input": type(parameter.declaration).__name__ if parameter.declaration is not None else None,
                    "source": parameter.source,
                    "availability": "external" if parameter.source in {"dependency", "native"} else "declared",
                }
                for parameter in self.plan.parameters
            ],
            "response": {
                "rich": declaration.policy.rich,
                "soft_messages": declaration.policy.soft_messages,
                "max_output_bytes": declaration.policy.max_output_bytes,
            },
            "payload": _type_name(declaration.payload) if declaration.payload else None,
            "ack": declaration.ack if declaration.event == "callback_query" else None,
            "filters": [_callable_name(item) for item in declaration.filters],
            "dynamic_filters": declaration.filter_factory is not None,
            "command_match": _callable_name(declaration.metadata["_command_filter"])
            if "_command_filter" in declaration.metadata
            else "native"
            if declaration.kind == "command"
            else None,
            "metadata": {
                name: _metadata_value(value) for name, value in declaration.metadata.items() if not name.startswith("_")
            },
        }


def _type_name(value: object) -> str:
    if value is inspect.Parameter.empty:
        return "untyped"
    return str(getattr(value, "__qualname__", value))


def _callable_name(value: object) -> str:
    return str(getattr(value, "__qualname__", type(value).__qualname__))


def _metadata_value(value: object) -> object:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, type):
        return _type_name(value)
    return {"dynamic": _callable_name(value)}


def source_of(handler: Callable[..., Any]) -> Source:
    try:
        return Source(inspect.getsourcefile(handler), inspect.getsourcelines(handler)[1])
    except OSError, TypeError:
        return Source(None, None)


def compile_feature(feature: Feature) -> tuple[tuple[CompiledHandler, ...], tuple[Diagnostic, ...]]:
    handlers: list[CompiledHandler] = []
    errors: list[Diagnostic] = []
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}", feature.key):
        errors.append(Diagnostic("feature-key", "Feature key must be a stable 1–64 character identifier", feature.key))
    # Base slots retain their order; replacing a method does not move its route.
    names = dict.fromkeys(name for base in reversed(type(feature).__mro__) for name in base.__dict__)
    events = Router().observers
    for name in names:
        raw = next(base.__dict__[name] for base in type(feature).__mro__ if name in base.__dict__)
        if isinstance(raw, staticmethod | classmethod):
            raw = raw.__func__
        if not inspect.isfunction(raw):
            inherited = any(
                getattr(base.__dict__.get(name), "__teleforge_declarations__", ()) for base in type(feature).__mro__[1:]
            )
            if inherited:
                errors.append(
                    Diagnostic(
                        "handler-shadowed", "An entrypoint must remain a method; use @disable", feature.key, name
                    )
                )
            continue
        bound = cast(Handler, getattr(feature, name))
        declarations = declarations_of(bound)
        if not declarations:
            continue
        source = source_of(bound)

        def error(code: str, message: str, name: str = name, source: Source = source) -> None:
            errors.append(Diagnostic(code, message, feature.key, name, source))

        if not inspect.iscoroutinefunction(bound) and any(item.kind != "card" for item in declarations):
            error("handler-async", "Entrypoint must be an async method")
            continue
        signature = inspect.signature(bound)
        try:
            namespace: dict[str, Any] = {}
            for base in reversed(type(feature).__mro__):
                namespace.update(vars(base))
                namespace[base.__name__] = base
            annotations = get_type_hints(bound, localns=namespace, include_extras=True)
        except (NameError, TypeError) as exc:
            error("annotation", f"Cannot resolve method annotations ({type(exc).__name__}); use importable types")
            continue
        for index, declaration in enumerate(declarations):
            plan, parameter_issues = compile_parameters(bound, declaration, annotations=annotations)
            for issue in parameter_issues:
                error(issue.code, issue.message)
            if declaration.event is not None and declaration.event not in events:
                error("event-name", f"Unknown native event '{declaration.event}'")
            if declaration.kind == "command":
                custom = "_command_filter" in declaration.metadata
                valid_names = all(
                    bool(alias) and len(alias) <= 100 if custom else bool(re.fullmatch(r"\w{1,32}", alias))
                    for alias in declaration.names
                )
                if not declaration.names or not valid_names:
                    error(
                        "command-name",
                        "Declare nonempty command names; native names use 1–32 letters, digits or underscores",
                    )
            if "card" in declaration.metadata or "card_action" in declaration.metadata:
                from .cards import CardError, validate_card

                try:
                    validate_card(bound)
                except (CardError, AttributeError, NameError, TypeError) as exc:
                    error("card-schema", str(exc))
            suffix = f":{index + 1}" if len(declarations) > 1 else ""
            handlers.append(
                CompiledHandler(
                    feature,
                    name,
                    f"{feature.key}.{name}{suffix}",
                    bound,
                    declaration,
                    signature,
                    annotations,
                    source,
                    plan,
                )
            )
    identities: dict[tuple[str, str], CompiledHandler] = {}
    for compiled in handlers:
        declaration = compiled.declaration
        identity: tuple[str, str] | None = None
        if "step" in declaration.metadata:
            identity = ("step", str(declaration.metadata["step"]))
        elif "card_action" in declaration.metadata:
            identity = ("action", str(declaration.metadata.get("card_action_key", "")))
        elif declaration.kind == "job" and declaration.names:
            identity = ("job", declaration.names[0])
        if identity is not None:
            if identity in identities:
                errors.append(
                    Diagnostic(
                        f"duplicate-{identity[0]}",
                        f"Declared {identity[0]} identities must be unique; duplicate '{identity[1]}'",
                        feature.key,
                        compiled.name,
                        compiled.source,
                    )
                )
            identities[identity] = compiled
    return tuple(handlers), tuple(errors)

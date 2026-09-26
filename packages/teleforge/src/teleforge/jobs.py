"""Typed job declarations for application-owned workers and atomic enqueue operations."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel

from .app import App
from .declarations import Declaration, attach_declaration

_Handler = TypeVar("_Handler", bound=Callable[..., Any])
JobHandler = Callable[..., Awaitable[object]]


class JobAdapter(Protocol):
    """The application owns leases, retries, transactions and handler execution context."""

    def register(self, name: str, handler: JobHandler) -> object: ...


def job(name: str, *, payload: type[BaseModel], argument: str = "payload") -> Callable[[_Handler], _Handler]:
    """Declare a job; enqueue through the application's transaction, never this decorator."""
    if not name or not argument.isidentifier():
        raise ValueError("Jobs need a nonempty name and a valid payload argument name")
    if not inspect.isclass(payload) or not issubclass(payload, BaseModel):
        raise TypeError("A job payload must be a Pydantic model")

    def decorate(method: _Handler) -> _Handler:
        if argument not in inspect.signature(method).parameters:
            raise ValueError(f"Job method needs a {argument!r} parameter")
        attach_declaration(
            method,
            Declaration(kind="job", names=(name,), metadata={"payload": payload, "argument": argument}),
        )
        return method

    return decorate


def bind_jobs(app: App, adapter: JobAdapter) -> tuple[str, ...]:
    """Register declared handlers with a worker. This does not schedule or commit any job."""
    handlers = app.iter_handlers("job")
    identities = [f"{handler.feature.key}.{handler.declaration.names[0]}" for handler in handlers]
    if len(identities) != len(set(identities)):
        raise ValueError("Declared job names must be unique within each feature")
    keys = []
    for compiled, key in zip(handlers, identities, strict=True):
        model = cast(type[BaseModel], compiled.declaration.metadata["payload"])
        argument = cast(str, compiled.declaration.metadata["argument"])

        def bind(method: Callable[..., Any], model: type[BaseModel], argument: str) -> JobHandler:
            async def invoke(payload: Mapping[str, object], **context: object) -> object:
                parsed = model.model_validate_json(json.dumps(dict(payload), allow_nan=False), strict=True)
                if argument in context:
                    raise TypeError("Worker context cannot replace the validated job payload")
                result: object = method(**{argument: parsed, **context})
                return await result if inspect.isawaitable(result) else result

            return invoke

        adapter.register(key, bind(compiled.handler, model, argument))
        keys.append(key)
    return tuple(keys)

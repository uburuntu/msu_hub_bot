"""Native HTTP adapters for feature methods; the application owns authentication and lifetime."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, cast

from .app import App
from .declarations import Declaration, attach_declaration

_Handler = TypeVar("_Handler", bound=Callable[..., Any])


class WebAdapter(Protocol):
    """Compatible with an application's native router or a small router adapter."""

    def add_route(self, method: str, path: str, handler: Callable[..., Any]) -> object: ...


def web(method: str, path: str) -> Callable[[_Handler], _Handler]:
    """Expose a native request handler; no implicit auth, body parsing or database choice."""
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ValueError("Unsupported HTTP method")
    if not path.startswith("/"):
        raise ValueError("An HTTP route needs an absolute application path")

    def decorate(handler: _Handler) -> _Handler:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError("HTTP feature methods must be async")
        attach_declaration(handler, Declaration(kind="web", names=(path,), metadata={"method": method, "path": path}))
        return handler

    return decorate


def bind_web(app: App, adapter: WebAdapter) -> None:
    """Attach feature methods to the application router and its existing middleware."""
    for compiled in app.iter_handlers("web"):
        method = cast(str, compiled.declaration.metadata["method"])
        path = cast(str, compiled.declaration.metadata["path"])
        adapter.add_route(method, path, compiled.handler)

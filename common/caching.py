"""Typing boundary for the untyped aiocache decorator used by provider lookups."""

from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

from aiocache import cached

_F = TypeVar("_F", bound=Callable[..., Awaitable[object]])


def cached_async(*, ttl: int, noself: bool = False) -> Callable[[_F], _F]:
    return cast(Callable[[_F], _F], cached(ttl=ttl, noself=noself))

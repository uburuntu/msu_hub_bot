"""Invocation facts for application middleware, without transaction assumptions."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

if TYPE_CHECKING:
    from .context import Context


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    owned: bool = True
    attempted: bool = False
    confirmed: bool = False
    uncertain: bool = False


@dataclass(frozen=True, slots=True)
class Presentation:
    kind: Literal["reply", "edit", "native"]
    attempted: bool
    confirmed: int
    uncertain: bool
    phase: str


@dataclass(frozen=True, slots=True)
class InputIssue:
    code: str
    params: tuple[tuple[str, str | int], ...]


@dataclass(frozen=True, slots=True)
class InvocationOutcome:
    """A snapshot of observed phases. A handler return never attests a commit."""

    handler_returned: bool = False
    acknowledgement: Acknowledgement | None = None
    presentations: tuple[Presentation, ...] = ()
    input_issue: InputIssue | None = None


class Invocation:
    """Shared holder under ``data['teleforge_invocation']`` across aiogram copies."""

    context: Context | None = None

    @property
    def outcome(self) -> InvocationOutcome:
        return self.context.outcome if self.context is not None else InvocationOutcome()


class InvocationMiddleware(BaseMiddleware):
    """Install before host observers that need outcomes after handled input issues."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["teleforge_invocation"] = Invocation()
        return await handler(event, data)


def attach_outcome(error: BaseException, outcome: InvocationOutcome) -> None:
    """Preserve immutable phase facts on the original exception, including cancellation."""
    error.teleforge_outcome = outcome  # type: ignore[attr-defined]

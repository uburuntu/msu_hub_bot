"""Named, typed steps over the application's aiogram FSM; no replay or separate storage."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from pydantic import BaseModel, ValidationError

from .context import Context
from .declarations import Declaration, attach_declaration, declarations_of
from .feature import Feature

_Handler = TypeVar("_Handler", bound=Callable[..., Any])
_DRAFT = "__teleforge_draft__"
_PREFIX = "teleforge:"


class ConversationError(ValueError):
    """The saved step, draft, or application FSM scope is invalid."""


def _state(ctx: Context) -> FSMContext:
    state = ctx.data.get("state")
    if not isinstance(state, FSMContext):
        raise ConversationError("A conversation requires application-provided aiogram FSM storage")
    message, user = ctx.message, ctx.user
    if not isinstance(message, Message) or user is None:
        raise ConversationError("A conversation requires a chat message and a human actor")
    if message.direct_messages_topic is not None:
        # aiogram USER_IN_TOPIC keys forum message_thread_id only.
        # A direct-message topic cannot safely fall back to the general chat.
        raise ConversationError("Direct-message topics require an explicit application conversation adapter")
    key = state.key
    if (key.bot_id, key.chat_id, key.user_id, key.thread_id, key.business_connection_id) != (
        ctx.bot.id,
        message.chat.id,
        user.id,
        message.message_thread_id if message.is_topic_message else None,
        message.business_connection_id,
    ):
        raise ConversationError("FSM storage must isolate this bot, actor, chat, topic and business connection")
    return state


def _name(feature: Feature, step_name: str) -> str:
    return f"{_PREFIX}{feature.key}:{step_name}"


def _step(method: Callable[..., Any]) -> tuple[Feature, Declaration]:
    feature = getattr(method, "__self__", None)
    if not isinstance(feature, Feature):
        raise ConversationError("A conversation destination must be a bound feature step")
    declarations = [item for item in declarations_of(method) if "step" in item.metadata]
    if len(declarations) != 1:
        raise ConversationError("A conversation destination must have exactly one @step declaration")
    return feature, declarations[0]


def _draft[DraftModel: BaseModel](model: type[DraftModel], value: object) -> DraftModel:
    try:
        return model.model_validate_json(json.dumps(value, allow_nan=False), strict=True)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ConversationError("The conversation draft does not match its declared model") from exc


async def _read_draft[DraftModel: BaseModel](
    ctx: Context, model: type[DraftModel], *, expected_state: str | None = None
) -> DraftModel:
    if not inspect.isclass(model) or not issubclass(model, BaseModel):
        raise ConversationError("A conversation draft must use a Pydantic model")
    state = _state(ctx)
    current = await state.get_state()
    if expected_state is not None and current != expected_state:
        raise ConversationError("The conversation has already changed")
    if current is None or not current.startswith(_PREFIX):
        raise ConversationError("There is no active managed conversation step")
    envelope = (await state.get_data()).get(_DRAFT)
    if (
        not isinstance(envelope, dict)
        or not isinstance(envelope.get("feature"), str)
        or not isinstance(envelope.get("step"), str)
        or not envelope["feature"]
        or not envelope["step"]
        or current != f"{_PREFIX}{envelope['feature']}:{envelope['step']}"
        or "value" not in envelope
    ):
        raise ConversationError("The saved draft belongs to a different conversation step")
    return _draft(model, envelope["value"])


async def read_draft[DraftModel: BaseModel](ctx: Context, model: type[DraftModel]) -> DraftModel:
    """Read an active, scope-checked draft before leaving or releasing isolation.

    The caller selects the intended step with its route/state check. This reader
    verifies that the saved envelope agrees with the active managed state and
    strictly validates its value using the supplied model, without changing FSM data.
    """
    return await _read_draft(ctx, model)


async def enter(ctx: Context, destination: Callable[..., Any], draft: BaseModel) -> None:
    """Start or advance a named step while the application's FSM isolation is held."""
    feature, declaration = _step(destination)
    model = cast(type[BaseModel], declaration.metadata["draft"])
    if not isinstance(draft, model):
        raise ConversationError(f"Step requires a {model.__name__} draft")
    step_name = cast(str, declaration.metadata["step"])
    payload = _draft(model, draft.model_dump(mode="json")).model_dump(mode="json")
    state = _state(ctx)
    current = await state.get_state()
    if current is not None and not current.startswith(f"{_PREFIX}{feature.key}:"):
        raise ConversationError(
            "Another workflow is active; cancel or clear it explicitly before entering this feature"
        )
    await state.update_data({_DRAFT: {"feature": feature.key, "step": step_name, "value": payload}})
    await state.set_state(_name(feature, step_name))


async def leave(ctx: Context) -> None:
    """Leave a managed step, preserving unrelated application FSM data."""
    state = _state(ctx)
    current = await state.get_state()
    if current is not None and not current.startswith(_PREFIX):
        raise ConversationError("Cannot clear a conversation owned by another FSM workflow")
    values = await state.get_data()
    values.pop(_DRAFT, None)
    await state.set_state(None)
    await state.set_data(values)


def step(name: str, *, draft: type[BaseModel], event: str = "message") -> Callable[[_Handler], _Handler]:
    """Declare a step whose `draft` parameter receives a validated saved model."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
        raise ConversationError("Step names must start with a letter and contain letters, digits, '_' or '-'")
    if not inspect.isclass(draft) or not issubclass(draft, BaseModel):
        raise ConversationError("A step draft must be a Pydantic model")

    def decorate(method: _Handler) -> _Handler:
        def filters(feature: Feature) -> tuple[Callable[..., Any], ...]:
            return (StateFilter(_name(feature, name)),)

        async def hook(
            feature: Feature, ctx: Context, data: dict[str, Any], invoke: Callable[[], Awaitable[object]]
        ) -> object:
            data["draft"] = await _read_draft(ctx, draft, expected_state=_name(feature, name))
            return await invoke()

        attach_declaration(
            method,
            Declaration(
                kind="event",
                event=event,
                filter_factory=filters,
                hook=hook,
                metadata={"step": name, "draft": draft},
            ),
        )
        return method

    return decorate

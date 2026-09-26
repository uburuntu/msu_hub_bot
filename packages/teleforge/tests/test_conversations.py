from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, DirectMessagesTopic, Message, Update, User
from pydantic import BaseModel, ConfigDict

from teleforge import App, IsolationError
from teleforge.context import Context
from teleforge.conversations import ConversationError, enter, leave, read_draft, step
from teleforge.declarations import command, declarations_of
from teleforge.feature import Feature
from teleforge.testing import RecordingBot


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: int


class Titles(Feature, key="titles"):
    def __init__(self) -> None:
        self.seen: list[int] = []

    @step("title", draft=Draft)
    async def title(self, ctx: Context, draft: Draft) -> None:
        self.seen.append(draft.source_id)


def context(*, user: int = 7, topic: int = 5) -> Context:
    bot = RecordingBot()
    message = Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type="supergroup"),
        from_user=User(id=user, is_bot=False, first_name="Synthetic"),
        message_thread_id=topic,
        is_topic_message=True,
    )
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=42, chat_id=-100, user_id=user, thread_id=topic))
    return Context(bot, message, data={"state": state})


async def invoke(ctx: Context, feature: Titles) -> None:
    declaration = declarations_of(feature.title)[0]
    assert declaration.hook is not None

    async def call() -> object:
        return await feature.title(ctx, ctx.data["draft"])

    await declaration.hook(feature, ctx, ctx.data, call)


async def test_typed_step_runs_and_preserves_unrelated_fsm_data() -> None:
    ctx, feature = context(), Titles()
    state: FSMContext = ctx.data["state"]
    await state.update_data({"application": "preserved"})
    await enter(ctx, feature.title, Draft(source_id=12))
    assert await state.get_state() == "teleforge:titles:title"
    await invoke(ctx, feature)
    assert feature.seen == [12]
    await leave(ctx)
    assert await state.get_state() is None
    assert await state.get_data() == {"application": "preserved"}


@pytest.mark.parametrize("value", [{"source_id": "12"}, {"source_id": True}, {"source_id": 12, "extra": 1}, {}])
async def test_saved_draft_is_strictly_validated_before_handler(value: dict[str, Any]) -> None:
    ctx, feature = context(), Titles()
    await enter(ctx, feature.title, Draft(source_id=12))
    state: FSMContext = ctx.data["state"]
    await state.update_data({"__teleforge_draft__": {"feature": "titles", "step": "title", "value": value}})
    with pytest.raises(ConversationError, match="draft"):
        await invoke(ctx, feature)
    with pytest.raises(ConversationError, match="draft"):
        await read_draft(ctx, Draft)
    assert feature.seen == []


@pytest.mark.parametrize("field,value", [("user_id", 9), ("chat_id", -200), ("thread_id", None), ("bot_id", 99)])
async def test_fsm_key_must_match_actor_chat_topic_and_bot(field: str, value: int | None) -> None:
    ctx, feature = context(), Titles()
    values = {"bot_id": 42, "chat_id": -100, "user_id": 7, "thread_id": 5, field: value}
    ctx.data["state"] = FSMContext(MemoryStorage(), StorageKey(**values))  # type: ignore[arg-type]
    with pytest.raises(ConversationError, match="isolate"):
        await enter(ctx, feature.title, Draft(source_id=12))
    with pytest.raises(ConversationError, match="isolate"):
        await read_draft(ctx, Draft)


async def test_other_feature_or_step_draft_is_never_dispatched() -> None:
    ctx, feature = context(), Titles()
    await enter(ctx, feature.title, Draft(source_id=12))
    state: FSMContext = ctx.data["state"]
    await state.update_data({"__teleforge_draft__": {"feature": "other", "step": "title", "value": {"source_id": 12}}})
    with pytest.raises(ConversationError, match="different"):
        await invoke(ctx, feature)
    await state.set_state("native:other")
    with pytest.raises(ConversationError, match="another"):
        await leave(ctx)
    assert await state.get_state() == "native:other"


async def test_invalid_draft_type_does_not_change_existing_state() -> None:
    class OtherDraft(BaseModel):
        pass

    ctx, feature = context(), Titles()
    state: FSMContext = ctx.data["state"]
    with pytest.raises(ConversationError, match="Draft"):
        await enter(ctx, feature.title, OtherDraft())
    assert await state.get_state() is None


async def test_nonforum_message_thread_hint_does_not_create_a_topic_scope() -> None:
    ctx, feature = context(), Titles()
    ctx.event = ctx.event.model_copy(update={"is_topic_message": False})
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=42, chat_id=-100, user_id=7, thread_id=None))
    ctx.data["state"] = state
    await enter(ctx, feature.title, Draft(source_id=12))
    await invoke(ctx, feature)
    assert feature.seen == [12]


async def test_business_connection_must_match_fsm_storage_identity() -> None:
    ctx, feature = context(), Titles()
    ctx.event = ctx.event.model_copy(update={"business_connection_id": "business-a"})
    with pytest.raises(ConversationError, match="business connection"):
        await enter(ctx, feature.title, Draft(source_id=12))
    ctx.data["state"] = FSMContext(
        MemoryStorage(),
        StorageKey(bot_id=42, chat_id=-100, user_id=7, thread_id=5, business_connection_id="business-a"),
    )
    await enter(ctx, feature.title, Draft(source_id=12))
    await invoke(ctx, feature)
    assert feature.seen == [12]


async def test_direct_message_topic_cannot_silently_use_general_chat_state() -> None:
    ctx, feature = context(), Titles()
    ctx.event = ctx.event.model_copy(
        update={
            "is_topic_message": False,
            "message_thread_id": None,
            "direct_messages_topic": DirectMessagesTopic(topic_id=9),
        }
    )
    ctx.data["state"] = FSMContext(MemoryStorage(), StorageKey(bot_id=42, chat_id=-100, user_id=7))
    with pytest.raises(ConversationError, match="Direct-message topics"):
        await enter(ctx, feature.title, Draft(source_id=12))
    assert await ctx.data["state"].get_data() == {}


async def test_failed_state_write_cannot_dispatch_previous_step_with_new_draft() -> None:
    class Storage(MemoryStorage):
        fail = False

        async def set_state(self, key: StorageKey, state: Any = None) -> None:
            if self.fail:
                raise ConnectionError("state write failed")
            await super().set_state(key, state)

    class Workflow(Titles):
        @step("next", draft=Draft)
        async def next(self, ctx: Context, draft: Draft) -> None:
            self.seen.append(draft.source_id)

    ctx, feature, storage = context(), Workflow(), Storage()
    state = FSMContext(storage, StorageKey(bot_id=42, chat_id=-100, user_id=7, thread_id=5))
    ctx.data["state"] = state
    await enter(ctx, feature.title, Draft(source_id=12))
    storage.fail = True
    with pytest.raises(ConnectionError):
        await enter(ctx, feature.next, Draft(source_id=99))
    with pytest.raises(ConversationError, match="different"):
        await invoke(ctx, feature)
    assert feature.seen == []


@pytest.mark.parametrize("active", ["native:payment", "teleforge:another:title"])
async def test_enter_does_not_take_over_another_active_workflow(active: str) -> None:
    ctx, feature = context(), Titles()
    state: FSMContext = ctx.data["state"]
    await state.set_state(active)
    await state.set_data({"application": "keep", "__teleforge_draft__": {"feature": "another"}})
    before = await state.get_data()
    with pytest.raises(ConversationError, match="Another workflow"):
        await enter(ctx, feature.title, Draft(source_id=12))
    assert await state.get_state() == active
    assert await state.get_data() == before


async def test_read_draft_is_typed_and_does_not_modify_state() -> None:
    ctx, feature = context(), Titles()
    state: FSMContext = ctx.data["state"]
    await state.update_data({"application": "preserved"})
    await enter(ctx, feature.title, Draft(source_id=12))
    before = await state.get_data()
    draft = await read_draft(ctx, Draft)
    assert isinstance(draft, Draft) and draft.source_id == 12
    assert await state.get_state() == "teleforge:titles:title"
    assert await state.get_data() == before
    await leave(ctx)
    with pytest.raises(ConversationError, match="no active"):
        await read_draft(ctx, Draft)
    assert await state.get_data() == {"application": "preserved"}


@pytest.mark.parametrize("active", [None, "native:payment", "teleforge:other:title"])
async def test_read_draft_requires_active_state_and_matching_envelope(active: str | None) -> None:
    ctx, feature = context(), Titles()
    state: FSMContext = ctx.data["state"]
    await enter(ctx, feature.title, Draft(source_id=12))
    await state.set_state(active)
    with pytest.raises(ConversationError):
        await read_draft(ctx, Draft)


async def test_read_draft_rejects_the_wrong_requested_model() -> None:
    class OtherDraft(BaseModel):
        text: str

    ctx, feature = context(), Titles()
    await enter(ctx, feature.title, Draft(source_id=12))
    with pytest.raises(ConversationError, match="declared model"):
        await read_draft(ctx, OtherDraft)


async def test_read_draft_is_rejected_after_native_isolation_release() -> None:
    class Workflow(Titles):
        @command("open")
        async def open(self, ctx: Context) -> None:
            await enter(ctx, self.title, Draft(source_id=12))
            await ctx.release_isolation()
            await read_draft(ctx, Draft)

    ctx = context()
    event = ctx.event.model_copy(update={"text": "/open"})
    async with App(Workflow()) as app:
        with pytest.raises(IsolationError, match="after terminal"):
            await app.feed_update(ctx.bot, Update(update_id=1, message=event))

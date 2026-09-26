from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime
from typing import Annotated, ClassVar, Literal

import pytest
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    InlineKeyboardButton,
    Message,
    MessageEntity,
    Update,
    User,
)
from pydantic import AfterValidator, field_validator

from teleforge.app import App
from teleforge.binding import invoke_handler
from teleforge.cards import Button, Card, CardContent, CardError, CardRefreshError, action, card, prepare_card, show
from teleforge.context import Context
from teleforge.declarations import declarations_of, disable
from teleforge.delivery import DeliveryTarget, edit_response, send_response
from teleforge.feature import CompilationError, Feature, compile_feature
from teleforge.issues import ConfigurationError
from teleforge.testing import RecordingBot


def message(*, actor: int = 7, message_id: int = 10, topic: int = 5, chat_id: int = -100) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="supergroup"),
        from_user=User(id=actor, is_bot=actor == 42, first_name="Synthetic"),
        text="card",
        message_thread_id=topic,
        is_topic_message=True,
    )


class StaleClick(ValueError):
    pass


class Counter(Feature, key="counter"):
    def __init__(self) -> None:
        self.value = 0
        self.revision = 0
        self.ui_id = 0
        self.fail_render = False
        self.active = 0
        self.max_active = 0

    @card
    async def panel(self, ctx: Context, item: int) -> Card:
        if self.fail_render:
            raise RuntimeError("render failed")
        return Card(
            f"Item {item}: {self.value}",
            buttons=(
                (
                    Button("+", self.increase, revision=self.revision),
                    Button("-", self.decrease, revision=self.revision),
                ),
            ),
        )

    def guard(self, ctx: Context, revision: int) -> None:
        ui = ctx.message
        if ctx.user is None or ctx.user.id != 7 or not isinstance(ui, Message):
            raise PermissionError("actor")
        if (ui.chat.id, ui.message_id, ui.message_thread_id) != (-100, self.ui_id, 5):
            raise PermissionError("origin")
        if revision != self.revision:
            raise StaleClick("revision")

    async def change(self, ctx: Context, revision: int, delta: int) -> None:
        self.guard(ctx, revision)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            self.value += delta
            self.revision += 1
        finally:
            self.active -= 1

    @action(key="increase", card="panel")
    async def increase(self, ctx: Context, item: int, revision: int) -> None:
        await self.change(ctx, revision, 1)

    @action(key="decrease", card="panel")
    async def decrease(self, ctx: Context, item: int, revision: int) -> None:
        await self.change(ctx, revision, -1)


async def open_card(bot: RecordingBot, feature: Counter) -> tuple[Message, list[str]]:
    sent = await show(Context(bot, message()), feature.panel, item=3)
    assert isinstance(sent, Message)
    feature.ui_id = sent.message_id
    assert sent.reply_markup is not None
    callbacks = [button.callback_data for button in sent.reply_markup.inline_keyboard[0]]
    assert all(isinstance(value, str) for value in callbacks)
    return sent, callbacks  # type: ignore[return-value]


async def click(
    bot: RecordingBot, feature: Counter, ui: Message, value: str, *, method: str = "increase", actor: int = 7
) -> object:
    callback = CallbackQuery(
        id=value,
        from_user=User(id=actor, is_bot=False, first_name="Clicker"),
        chat_instance="test",
        message=ui,
        data=value,
    )
    declaration = declarations_of(getattr(feature, method))[0]
    assert declaration.filter_factory is not None
    matched = await declaration.filter_factory(feature)[0](callback)
    if matched is False:
        return False
    assert isinstance(matched, dict)
    compiled, errors = compile_feature(feature)
    assert not errors
    handler = next(item for item in compiled if item.name == method)
    return await invoke_handler(handler, callback, bot=bot, **matched)


async def test_action_changes_once_refreshes_keyboard_and_rejects_stale_click() -> None:
    bot, feature = RecordingBot(), Counter()
    ui, callbacks = await open_card(bot, feature)
    await click(bot, feature, ui, callbacks[0])
    assert feature.value == 1
    edits = [request for request in bot.requests if isinstance(request, EditMessageText)]
    assert len(edits) == 1 and edits[0].message_id == ui.message_id
    assert edits[0].reply_markup is not None
    assert edits[0].reply_markup.inline_keyboard[0][0].callback_data != callbacks[0]
    with pytest.raises(StaleClick):
        await click(bot, feature, ui, callbacks[0])
    assert feature.value == 1


async def test_concurrent_different_buttons_share_ui_lock() -> None:
    bot, feature = RecordingBot(), Counter()
    ui, callbacks = await open_card(bot, feature)
    results = await asyncio.gather(
        click(bot, feature, ui, callbacks[0]),
        click(bot, feature, ui, callbacks[1], method="decrease"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, StaleClick) for result in results) == 1
    assert feature.max_active == 1 and feature.revision == 1


@pytest.mark.parametrize("alteration", ["actor", "message", "topic", "chat"])
async def test_application_guards_and_native_ui_boundary(alteration: str) -> None:
    bot, feature = RecordingBot(), Counter()
    ui, callbacks = await open_card(bot, feature)
    update: dict[str, object] = {}
    if alteration == "message":
        update["message_id"] = ui.message_id + 1
    elif alteration == "topic":
        update["message_thread_id"] = 9
    elif alteration == "chat":
        update["chat"] = Chat(id=-200, type="supergroup")
    with pytest.raises((PermissionError, CardError)):
        await click(bot, feature, ui.model_copy(update=update), callbacks[0], actor=8 if alteration == "actor" else 7)
    assert feature.value == 0


async def test_invalid_typed_payload_is_not_forgiving() -> None:
    bot, feature = RecordingBot(), Counter()
    ui, callbacks = await open_card(bot, feature)
    prefix = callbacks[0].rsplit(":", 1)[0]
    assert await click(bot, feature, ui, prefix + ':["3",0]') is False
    assert await click(bot, feature, ui, prefix + ":[3,true]") is False
    assert await click(bot, feature, ui, prefix + ":[3]") is False
    assert feature.value == 0


async def test_successful_mutation_is_not_replayed_after_refresh_failure() -> None:
    bot, feature = RecordingBot(), Counter()
    ui, callbacks = await open_card(bot, feature)
    feature.fail_render = True
    with pytest.raises(CardRefreshError) as caught:
        await click(bot, feature, ui, callbacks[0])
    assert caught.value.handler_returned and isinstance(caught.value.__cause__, RuntimeError)
    assert feature.value == 1 and feature.revision == 1
    with pytest.raises(StaleClick):
        await click(bot, feature, ui, callbacks[0])
    assert len([request for request in bot.requests if isinstance(request, SendMessage)]) == 1


async def test_oversized_button_fails_before_sending() -> None:
    class Large(Feature):
        @card
        async def panel(self, ctx: Context) -> Card:
            return Card("large", buttons=((Button("run", self.run, value="x" * 70),),))

        @action(key="run", card="panel")
        async def run(self, ctx: Context, value: str) -> None:
            pass

    bot, feature = RecordingBot(), Large()
    with pytest.raises(CardError, match="64-byte"):
        await show(Context(bot, message()), feature.panel)
    assert not bot.requests


async def test_ordinary_override_keeps_card_and_action_declarations() -> None:
    class Child(Counter):
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            await self.change(ctx, revision, 2)

    bot, feature = RecordingBot(), Child()
    ui, callbacks = await open_card(bot, feature)
    await click(bot, feature, ui, callbacks[0])
    assert feature.value == 2


async def test_disabled_action_cannot_render_a_managed_button() -> None:
    class Child(Counter):
        @disable
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            pass

    with pytest.raises(CardError, match="card_action"):
        await open_card(RecordingBot(), Child())


async def test_read_only_refresh_acknowledges_early_and_coalesces_busy_clicks() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Refresh(Counter):
        @action(key="increase", card="panel", ack="early", coalesce=True)
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            assert any(isinstance(request, AnswerCallbackQuery) for request in ctx.bot.requests)
            entered.set()
            await release.wait()
            self.value += 1

    bot, feature = RecordingBot(), Refresh()
    ui, callbacks = await open_card(bot, feature)
    first = asyncio.create_task(click(bot, feature, ui, callbacks[0]))
    await entered.wait()
    await click(bot, feature, ui, callbacks[0])
    assert len([request for request in bot.requests if isinstance(request, AnswerCallbackQuery)]) == 2
    assert feature.value == 0
    release.set()
    await first
    assert feature.value == 1
    assert len([request for request in bot.requests if isinstance(request, EditMessageText)]) == 1


async def test_explicit_refresh_barrier_preserves_committed_action_without_edit() -> None:
    class Preview(Counter):
        @action(key="increase", card="panel", refresh=False)
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            await self.change(ctx, revision, 1)

    bot, feature = RecordingBot(), Preview()
    ui, callbacks = await open_card(bot, feature)
    await click(bot, feature, ui, callbacks[0])
    assert feature.value == 1
    assert not any(isinstance(request, EditMessageText) for request in bot.requests)


async def test_numeric_literal_payload_does_not_accept_boolean_or_float_equivalents() -> None:
    class LiteralCounter(Counter):
        @action(key="increase", card="panel")
        async def increase(self, ctx: Context, item: int, revision: Literal[0, 1]) -> None:
            await self.change(ctx, revision, 1)

    bot, feature = RecordingBot(), LiteralCounter()
    ui, callbacks = await open_card(bot, feature)
    prefix = callbacks[0].rsplit(":", 1)[0]
    assert await click(bot, feature, ui, prefix + ":[3,false]") is False
    assert await click(bot, feature, ui, prefix + ":[3,0.0]") is False
    assert feature.value == 0


async def test_renderer_defaults_are_bound_into_actions_before_action_defaults() -> None:
    class Defaults(Counter):
        @card
        async def panel(self, ctx: Context, item: int = 1) -> Card:
            return Card(f"Item {item}", buttons=((Button("+", self.increase, revision=0),),))

        @action(key="increase", card="panel")
        async def increase(self, ctx: Context, item: int = 2, revision: int = 0) -> None:
            self.value = item

    bot, feature = RecordingBot(), Defaults()
    sent = await show(Context(bot, message()), feature.panel)
    assert isinstance(sent, Message) and sent.reply_markup is not None
    payload = sent.reply_markup.inline_keyboard[0][0].callback_data
    assert payload is not None
    await click(bot, feature, sent, payload)
    assert feature.value == 1


async def test_unknown_renderer_argument_fails_before_send() -> None:
    with pytest.raises(CardError, match="Unknown"):
        await show(Context(RecordingBot(), message()), Counter().panel, item=3, typo=4)


async def test_card_coalescing_is_owned_by_each_feature_instance() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Refresh(Counter):
        def __init__(self, *, pause: bool) -> None:
            super().__init__()
            self.pause = pause

        @action(key="increase", card="panel", coalesce=True)
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            if self.pause:
                entered.set()
                await release.wait()
            self.value += 1

    first, second = Refresh(pause=True), Refresh(pause=False)
    first_bot, second_bot = RecordingBot(), RecordingBot()
    first_ui, first_callbacks = await open_card(first_bot, first)
    second_ui, second_callbacks = await open_card(second_bot, second)
    assert first_ui.message_id == second_ui.message_id
    pending = asyncio.create_task(click(first_bot, first, first_ui, first_callbacks[0]))
    try:
        await entered.wait()
        await click(second_bot, second, second_ui, second_callbacks[0])
        assert second.value == 1
    finally:
        release.set()
        await pending


async def test_validator_function_addresses_do_not_change_button_identity() -> None:
    def application() -> Counter:
        def validate(value: int) -> int:
            return value

        class Typed(Counter, key="stable"):
            Revision = Annotated[int, AfterValidator(validate)]

            @action(key="increase", card="panel")
            async def increase(self, ctx: Context, item: int, revision: Revision) -> None:
                await self.change(ctx, revision, 1)

        return Typed()

    first, second = application(), application()
    _, before_restart = await open_card(RecordingBot(), first)
    bot = RecordingBot()
    ui, after_restart = await open_card(bot, second)
    assert before_restart == after_restart
    await click(bot, second, ui, before_restart[0])
    assert second.value == 1


async def test_cancelled_waiter_releases_only_its_own_card_lock_registration() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(Counter):
        @action(key="increase", card="panel")
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            entered.set()
            await release.wait()
            self.value += 1

    bot, feature = RecordingBot(), Waiting()
    ui, callbacks = await open_card(bot, feature)
    first = asyncio.create_task(click(bot, feature, ui, callbacks[0]))
    await entered.wait()
    waiter = asyncio.create_task(click(bot, feature, ui, callbacks[0]))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await first
    await click(bot, feature, ui, callbacks[0])
    assert feature.value == 2


async def test_transforming_callback_validator_cannot_retarget_the_displayed_record() -> None:
    def change_identity(value: int) -> int:
        return value + 1

    class Transforming(Counter):
        Item = Annotated[int, AfterValidator(change_identity)]

        @card
        async def panel(self, ctx: Context, item: Item) -> Card:
            return Card(f"Record {item}", buttons=((Button("+", self.increase, revision=0),),))

        @action(key="increase", card="panel")
        async def increase(self, ctx: Context, item: Item, revision: int) -> None:
            self.value = item

    bot = RecordingBot()
    with pytest.raises(CardError, match="normalize before building"):
        await open_card(bot, Transforming())
    assert not bot.requests


def payload_of(content: CardContent) -> str:
    payload = content["reply_markup"].inline_keyboard[0][0].callback_data
    assert payload is not None
    return payload


def update_for(payload: str, *, update_id: int = 1, actor: int = 7) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=User(id=actor, is_bot=False, first_name="Synthetic"),
            chat_instance="test",
            message=message(actor=42),
            data=payload,
        ),
    )


class CardStore:
    def __init__(self) -> None:
        self.value = 0
        self.rendered: list[int] = []


async def test_stable_key_replays_after_method_renames_and_dependency_additions() -> None:
    class Before(Feature, key="stable"):
        @card
        def panel(self, item: int) -> Card:
            return Card("Before", buttons=((Button("+", self.increase, delta=1),),))

        @action(key="increase", card="panel")
        async def increase(self, item: int, delta: int) -> None:
            pass

    class After(Feature, key="stable"):
        @card
        def renamed_panel(self, item: int, *, store: CardStore) -> Card:
            store.rendered.append(item)
            return Card(f"Value {store.value}", buttons=((Button("+", self.renamed_action, delta=1),),))

        @action(key="increase", card="renamed_panel")
        async def renamed_action(self, item: int, delta: int, *, store: CardStore) -> None:
            assert item == 3
            store.value += delta

    feature, store = After(), CardStore()
    old = await prepare_card(Before().panel, item=3)
    fresh = await prepare_card(feature.renamed_panel, item=3, data={"store": store})
    assert payload_of(old) == payload_of(fresh)
    app, bot = App(feature, data={"store": store, "item": "middleware must not shadow payload"}), RecordingBot()
    try:
        await app.feed_update(bot, update_for(payload_of(old)))
        assert store.value == 1 and store.rendered == [3, 3]
        edits = [request for request in bot.requests if isinstance(request, EditMessageText)]
        assert len(edits) == 1 and edits[0].text == "Value 1"
    finally:
        await app.aclose()


async def test_eventless_worker_preparation_uses_native_delivery_and_borrowed_media() -> None:
    bot = RecordingBot()
    stream = io.BytesIO(b"prepared document")
    native = InlineKeyboardButton(text="Native", callback_data="application:1")
    try:
        content = await prepare_card(
            Card(
                "Worker output",
                document=stream,
                entities=(MessageEntity(type="bold", offset=0, length=6),),
                buttons=((native,),),
            )
        )
        assert content["document"] is stream and not stream.closed and not bot.requests
        sent = await send_response(bot, DeliveryTarget(chat_id=-100, thread_id=5), **content, fixed=True)
        assert isinstance(sent, Message)
        assert sent.reply_markup is not None and sent.reply_markup.inline_keyboard[0][0] == native
        assert not stream.closed
        updated = await prepare_card(Card("Updated", buttons=((native,),)))
        await edit_response(bot, DeliveryTarget(chat_id=-100, message_id=sent.message_id, kind="document"), **updated)
        assert any(b"prepared document" in uploads.values() for uploads in bot.recording.uploads)
    finally:
        stream.close()


async def test_plain_renderer_has_explicit_dependencies_without_an_invocation() -> None:
    def render(item: int, *, store: CardStore) -> Card:
        store.rendered.append(item)
        return Card(f"Item {item}")

    store = CardStore()
    prepared = await prepare_card(render, item=4, data={"store": store})
    assert prepared["text"] == "Item 4" and store.rendered == [4]
    with pytest.raises(CardError, match="Missing renderer dependency"):
        await prepare_card(render, item=4)
    with pytest.raises(ConfigurationError, match="declared type"):
        await prepare_card(render, item=4, data={"store": object()})
    with pytest.raises(CardError, match="Unknown"):
        await prepare_card(render, item=4, store=store)
    with pytest.raises(CardError, match="explicit invocation context"):
        await prepare_card(Counter().panel, item=4)


@pytest.mark.parametrize("problem", ["missing", "schema"])
async def test_static_card_errors_agree_between_check_router_and_preparation(problem: str) -> None:
    class Broken(Feature):
        @card
        def panel(self, item: str) -> Card:
            return Card("Broken", buttons=((Button("Go", self.go),),))

        @action(key="go", card="missing" if problem == "missing" else "panel")
        async def go(self, item: int) -> None:
            pass

    feature = Broken()
    app = App(feature)
    errors = app.check()
    assert errors
    with pytest.raises(CompilationError) as built:
        app.build_router()
    with pytest.raises(CardError) as prepared:
        await prepare_card(feature.panel, item="1")
    assert any(error.message == str(prepared.value) for error in errors)
    assert built.value.diagnostics == errors


@pytest.mark.parametrize("cross_feature", [False, True])
async def test_actual_target_lock_spans_renderers_and_features(cross_feature: bool) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    active = maximum = calls = 0

    async def mutate() -> None:
        nonlocal active, maximum, calls
        active += 1
        maximum = max(active, maximum)
        calls += 1
        try:
            entered.set()
            await release.wait()
        finally:
            active -= 1

    class Panels(Feature, key="panels"):
        @card
        def left(self) -> Card:
            return Card("Left", buttons=((Button("Left", self.to_left),),))

        @card
        def right(self) -> Card:
            return Card("Right", buttons=((Button("Right", self.to_right),),))

        @action(key="left", card="left")
        async def to_left(self) -> None:
            await mutate()

        @action(key="right", card="right")
        async def to_right(self) -> None:
            await mutate()

    class Other(Panels, key="other"):
        pass

    first_feature, other_feature = Panels(), Other()
    right_feature = other_feature if cross_feature else first_feature
    app = App(first_feature, other_feature) if cross_feature else App(first_feature)
    bot = RecordingBot()
    first = asyncio.create_task(app.feed_update(bot, update_for(payload_of(await prepare_card(first_feature.left)))))
    await entered.wait()
    second = asyncio.create_task(
        app.feed_update(bot, update_for(payload_of(await prepare_card(right_feature.right)), update_id=2, actor=8))
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert calls == 1
    finally:
        release.set()
        await asyncio.gather(first, second)
        await app.aclose()
    assert calls == 2 and maximum == 1
    assert len([request for request in bot.requests if isinstance(request, EditMessageText)]) == 2


async def test_preview_delivery_barrier_survives_preparation_and_explicit_refresh() -> None:
    class Preview(Feature):
        def __init__(self) -> None:
            self.preview_delivered = False
            self.submissions = 0

        @card
        def panel(self) -> Card:
            return Card("Exact preview", buttons=((Button("Submit", self.submit),),))

        @action(key="submit", card="panel", refresh=False)
        async def submit(self) -> None:
            if not self.preview_delivered:
                raise PermissionError("The exact preview has not been delivered")
            self.submissions += 1

    feature, bot = Preview(), RecordingBot()
    prepared = await prepare_card(feature.panel)
    app = App(feature)
    try:
        with pytest.raises(PermissionError, match="exact preview"):
            await app.feed_update(bot, update_for(payload_of(prepared)))
        assert feature.submissions == 0
        await edit_response(bot, DeliveryTarget(chat_id=-100, message_id=10, kind="text"), **prepared)
        feature.preview_delivered = True
        await app.feed_update(bot, update_for(payload_of(prepared), update_id=2))
        assert feature.submissions == 1
        assert len([request for request in bot.requests if isinstance(request, EditMessageText)]) == 1
    finally:
        await app.aclose()


class NativeAction(CallbackData, prefix="native"):
    item: int
    delta: int = 1
    validations: ClassVar[int] = 0

    @field_validator("item")
    @classmethod
    def count_validation(cls, value: int) -> int:
        cls.validations += 1
        return value


async def test_native_schema_preserves_old_wire_after_renames_and_dependency_additions() -> None:
    class Before(Feature, key="native-before"):
        @card
        def panel(self, item: int) -> Card:
            return Card("Before", buttons=((Button("+", self.increase, delta=1),),))

        @action(key="increase", card="panel", payload=NativeAction)
        async def increase(self, item: int, delta: int) -> None:
            pass

    class After(Feature, key="native-after"):
        @card
        def renamed_panel(self, item: int, *, store: CardStore) -> Card:
            store.rendered.append(item)
            native = InlineKeyboardButton(text="+", callback_data=NativeAction(item=item).pack())
            return Card(f"Value {store.value}", buttons=((native,),))

        @action(key="renamed", card="renamed_panel", payload=NativeAction, ack="early")
        async def renamed_action(self, item: int, delta: int, *, store: CardStore) -> None:
            assert item == 3
            store.value += delta

    old = await prepare_card(Before().panel, item=3)
    assert payload_of(old) == "native:3:1"
    feature, store = After(), CardStore()
    assert payload_of(await prepare_card(feature.renamed_panel, item=3, data={"store": store})) == payload_of(old)
    app = App(feature, data={"store": store, "item": 999, "delta": 999})
    bot = RecordingBot()
    NativeAction.validations = 0
    try:
        await app.feed_update(bot, update_for(payload_of(old)))
        assert store.value == 1 and store.rendered == [3, 3]
        # One unpack and one new native button construction; binding/rendering
        # do not run the native validator again on an already validated payload.
        assert NativeAction.validations == 2
        assert isinstance(bot.requests[0], AnswerCallbackQuery)
        edit = next(request for request in bot.requests if isinstance(request, EditMessageText))
        assert edit.text == "Value 1" and edit.message_id == 10
        assert edit.reply_markup is not None
        assert edit.reply_markup.inline_keyboard[0][0].callback_data == "native:3:1"
    finally:
        await app.aclose()


@pytest.mark.parametrize("wire", ["native:x:1", "native:3", "native:3:1:2", "other:3:1", "native:3:" + "1" * 60])
async def test_native_invalid_wire_never_invokes_action_or_renderer(wire: str) -> None:
    class Native(Feature):
        @card
        def panel(self, item: int) -> Card:
            pytest.fail("renderer must not run")

        @action(key="go", card="panel", payload=NativeAction)
        async def go(self, item: int) -> None:
            pytest.fail("action must not run")

    app, bot = App(Native()), RecordingBot()
    try:
        await app.feed_update(bot, update_for(wire))
        assert not bot.requests
    finally:
        await app.aclose()


@pytest.mark.parametrize("field", ["missing", "type"])
async def test_native_card_schema_errors_agree_before_send(field: str) -> None:
    class Wrong(CallbackData, prefix="wrong"):
        item: str

    class Missing(CallbackData, prefix="missing"):
        other: int

    class Broken(Feature):
        @card
        def panel(self, item: int) -> Card:
            return Card("Broken", buttons=((Button("Go", self.go),),))

        @action(key="go", card="panel", payload=Missing if field == "missing" else Wrong)
        async def go(self) -> None:
            pass

    feature = Broken()
    app = App(feature)
    errors = app.check()
    assert errors
    with pytest.raises(CompilationError):
        app.build_router()
    with pytest.raises(CardError, match="native CallbackData field") as prepared:
        await prepare_card(feature.panel, item=1)
    assert any(error.message == str(prepared.value) for error in errors)


async def test_native_button_rejects_unknown_arguments_and_wire_overflow() -> None:
    class Label(CallbackData, prefix="label"):
        value: str

    class Native(Feature):
        @card
        def panel(self) -> Card:
            return Card("Panel")

        @action(key="go", card="panel", payload=Label)
        async def go(self, value: str) -> None:
            pass

    feature = Native()
    with pytest.raises(CardError, match="Unknown"):
        await prepare_card(Card(buttons=((Button("Go", feature.go, value="x", typo=1),),)))
    with pytest.raises(CardError, match="native card"):
        await prepare_card(Card(buttons=((Button("Go", feature.go, value="x" * 70),),)))


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("source", ["inline", "inaccessible", "other-bot", "missing-author"])
async def test_unavailable_card_source_guides_without_action_or_presentation(native: bool, source: str) -> None:
    class Guarded(Feature):
        @card
        def panel(self, item: int) -> Card:
            pytest.fail("unavailable source must not reach renderer")

        @action(key="go", card="panel", payload=NativeAction if native else None)
        async def go(self, item: int) -> None:
            pytest.fail("unavailable source must not reach domain action")

    feature = Guarded()
    data = payload_of(await prepare_card(Card(buttons=((Button("Go", feature.go, item=3),),))))
    incoming = update_for(data)
    query = incoming.callback_query
    assert query is not None
    if source == "inline":
        query = query.model_copy(update={"message": None, "inline_message_id": "inline-card"})
    elif source == "inaccessible":
        query = query.model_copy(
            update={"message": InaccessibleMessage(chat=Chat(id=-100, type="supergroup"), message_id=10, date=0)}
        )
    else:
        ui = message(actor=99) if source == "other-bot" else message().model_copy(update={"from_user": None})
        query = query.model_copy(update={"message": ui})
    app = App(feature, input_formatter=lambda issue: "Open again: " + issue.code)
    bot = RecordingBot()
    outcomes = []
    dispatcher = app.create_dispatcher()

    async def observe(handler, event, values):
        try:
            return await handler(event, values)
        finally:
            outcomes.append(values["teleforge_invocation"].outcome)

    dispatcher.callback_query.middleware(observe)
    try:
        await dispatcher.feed_update(bot, incoming.model_copy(update={"callback_query": query}))
        assert len(bot.requests) == 1
        assert isinstance(bot.requests[0], AnswerCallbackQuery)
        assert bot.requests[0].text == "Open again: callback-invalid" and bot.requests[0].show_alert
        assert not outcomes[0].handler_returned and not outcomes[0].presentations
        assert outcomes[0].acknowledgement.confirmed and outcomes[0].input_issue.code == "callback-invalid"
    finally:
        await app.aclose()

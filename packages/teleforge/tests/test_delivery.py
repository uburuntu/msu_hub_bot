from __future__ import annotations

import asyncio
import io
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import (
    EditMessageCaption,
    EditMessageMedia,
    EditMessageText,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendRichMessage,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichMessage,
    Message,
    MessageEntity,
    PhotoSize,
    Update,
    User,
)
from aiogram.utils.formatting import Bold, Text

from teleforge.app import App
from teleforge.context import CallbackContext, Context, MessageContext, context_for
from teleforge.declarations import callback, command
from teleforge.delivery import (
    DeliveryError,
    DeliveryTarget,
    ResponsePolicy,
    edit_response,
    send_response,
)
from teleforge.feature import Feature
from teleforge.formatting import ResponseError, ResponseLimitError, format_text, split_text, units
from teleforge.testing import RecordingBot, RecordingSession


def message(**changes: object) -> Message:
    fields: dict[str, object] = {
        "message_id": 10,
        "date": datetime.now(UTC),
        "chat": Chat(id=-100123, type="supergroup"),
        "from_user": User(id=7, is_bot=False, first_name="Author"),
        "text": "source",
        "is_topic_message": True,
        "message_thread_id": 31,
        "business_connection_id": "business-A",
    }
    fields.update(changes)
    return Message.model_validate(fields)


def query(ui: Message | None = None, **changes: object) -> CallbackQuery:
    fields: dict[str, object] = {
        "id": "query-1",
        "chat_instance": "opaque-not-a-chat-id",
        "from_user": User(id=88, is_bot=False, first_name="Clicker"),
        "message": ui or message(from_user=User(id=42, is_bot=True, first_name="Bot")),
        "data": "button:1",
    }
    fields.update(changes)
    return CallbackQuery.model_validate(fields)


def markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Next", callback_data="next")]])


@pytest.mark.asyncio
async def test_literal_text_overrides_bot_parse_mode_and_preserves_scope() -> None:
    bot = RecordingBot()
    bot.default = DefaultBotProperties(parse_mode="HTML")
    result = await send_response(bot, message(), "<b>literal</b>")
    assert isinstance(result, Message)
    method = bot.requests[0]
    assert isinstance(method, SendMessage)
    assert method.text == "<b>literal</b>"
    assert method.parse_mode is None
    assert method.entities == []
    assert method.message_thread_id == 31
    assert method.business_connection_id == "business-A"
    assert method.reply_parameters and method.reply_parameters.message_id == 10


@pytest.mark.asyncio
async def test_text_splitting_preserves_every_character_and_utf16_entities() -> None:
    bot = RecordingBot()
    original = "🦊" * 3000
    await send_response(bot, message(), Text(Bold(original)), policy=ResponsePolicy(rich=False))
    parts = [method for method in bot.requests if isinstance(method, SendMessage)]
    assert len(parts) == 2
    assert "".join(part.text for part in parts) == original
    assert all(units(part.text) <= 4096 for part in parts)
    assert [part.entities[0].length for part in parts if part.entities] == [4096, 1904]
    assert parts[1].reply_parameters and parts[1].reply_parameters.message_id == 101


@pytest.mark.parametrize(
    "entity", [MessageEntity(type="bold", offset=1, length=1), MessageEntity(type="bold", offset=0, length=1)]
)
def test_entity_cannot_split_surrogate_pair(entity: MessageEntity) -> None:
    with pytest.raises(ResponseError, match="Unicode"):
        format_text("🦊", [entity], 4096)


def test_indivisible_link_falls_back_without_destroying_destination() -> None:
    content = "x" * 5000
    formatted = format_text(
        content, [MessageEntity(type="text_link", offset=0, length=5000, url="https://example.test")], 10000
    )
    # Text links are splittable and retain their destination on each chunk.
    parts = list(split_text(formatted))
    assert len(parts) == 2
    assert all(part.entities[0].url == "https://example.test" for part in parts)


@pytest.mark.asyncio
async def test_soft_budget_becomes_complete_file_with_final_keyboard() -> None:
    bot = RecordingBot()
    text = "bounded " * 1600
    keyboard = markup()
    await send_response(bot, message(), text, policy=ResponsePolicy(rich=False, soft_messages=1), reply_markup=keyboard)
    assert len(bot.requests) == 1
    assert isinstance(bot.requests[0], SendDocument)
    assert bot.recording.uploads[0]["document"] == text.encode()
    assert bot.requests[0].reply_markup == keyboard


@pytest.mark.asyncio
async def test_output_bytes_are_checked_before_any_media_or_text_write() -> None:
    bot = RecordingBot()
    with pytest.raises(ResponseLimitError):
        await send_response(bot, message(), "12345", photo=b"123456", policy=ResponsePolicy(max_output_bytes=10))
    assert bot.requests == []


@pytest.mark.parametrize("operation", ["reply", "edit"])
@pytest.mark.parametrize("oversize", ["label", "url", "metadata", "shape"])
async def test_owned_markup_limits_fail_before_dispatch_sends(operation: str, oversize: str) -> None:
    if oversize == "label":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x" * 1000, callback_data="ok")]])
    elif oversize == "url":
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="open", url="https://example.test/" + "x" * 1000)]]
        )
    elif oversize == "metadata":
        keyboard = markup().model_copy(update={"future_metadata": {"description": "x" * 1000}})
    else:
        button = InlineKeyboardButton(text="small", callback_data="ok")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[button] for _ in range(4000)])

    class Keyboard(Feature):
        @command("run")
        async def run(self, ctx: MessageContext) -> None:
            await getattr(ctx, operation)(
                "x",
                policy=ResponsePolicy(max_output_bytes=1_000_000 if oversize == "shape" else 128),
                reply_markup=keyboard,
            )

    bot = RecordingBot()
    async with App(Keyboard()) as app:
        with pytest.raises(ResponseLimitError) as caught:
            await app.feed_update(bot, Update(update_id=1, message=message(text="/run")))
    assert "structural" in str(caught.value) if oversize == "shape" else "byte budget" in str(caught.value)
    assert bot.requests == []
    assert not caught.value.teleforge_outcome.presentations[0].attempted


@pytest.mark.parametrize("operation", ["reply", "edit"])
async def test_markup_body_and_upload_share_one_output_budget(operation: str) -> None:
    keyboard = markup()
    owned = len(keyboard.model_dump_json(exclude_none=True).encode())
    total = owned + len(b"caption") + len(b"photo")
    source = message(text=None, photo=[PhotoSize(file_id="photo", file_unique_id="photo", width=10, height=10)])
    bot = RecordingBot()
    call = send_response if operation == "reply" else edit_response
    with pytest.raises(ResponseLimitError):
        await call(
            bot,
            source,
            "caption",
            photo=b"photo",
            reply_markup=keyboard,
            policy=ResponsePolicy(rich=False, max_output_bytes=total - 1),
        )
    assert bot.requests == []
    await call(
        bot,
        source,
        "caption",
        photo=b"photo",
        reply_markup=keyboard,
        policy=ResponsePolicy(rich=False, max_output_bytes=total),
    )
    assert len(bot.requests) == 1 and bot.requests[0].reply_markup == keyboard


async def test_markup_reserves_bytes_from_complete_rich_replacement() -> None:
    keyboard = markup()
    owned = len(keyboard.model_dump_json(exclude_none=True).encode())
    rich = InputRichMessage(html="x" * 300)
    target = DeliveryTarget(chat_id=1, message_id=10, kind="rich")
    bot = RecordingBot()
    with pytest.raises(ResponseLimitError):
        await edit_response(
            bot, target, rich_message=rich, reply_markup=keyboard, policy=ResponsePolicy(max_output_bytes=owned + 299)
        )
    assert bot.requests == []
    bot.recording.responses.append(message())
    await edit_response(
        bot, target, rich_message=rich, reply_markup=keyboard, policy=ResponsePolicy(max_output_bytes=owned + 300)
    )
    assert len(bot.requests) == 1


async def test_markup_is_snapshotted_before_media_preparation_yields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from teleforge import delivery

    entered, release = asyncio.Event(), asyncio.Event()

    async def snapshot(path: Path, limit: int) -> bytes:
        entered.set()
        await release.wait()
        return b"file"

    monkeypatch.setattr(delivery, "_path_snapshot", snapshot)
    keyboard, bot = markup(), RecordingBot()
    task = asyncio.create_task(
        send_response(
            bot,
            message(),
            document=tmp_path / "file",
            reply_markup=keyboard,
            policy=ResponsePolicy(max_output_bytes=1024),
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    keyboard.inline_keyboard[0].append(InlineKeyboardButton(text="x" * 10_000, callback_data="later"))
    release.set()
    await task
    assert len(bot.requests[0].reply_markup.inline_keyboard[0]) == 1


async def test_split_response_counts_markup_once_and_attaches_it_to_final_message() -> None:
    keyboard, bot = markup(), RecordingBot()
    owned = len(keyboard.model_dump_json(exclude_none=True).encode())
    await send_response(
        bot,
        message(),
        "x" * 5000,
        reply_markup=keyboard,
        policy=ResponsePolicy(rich=False, max_output_bytes=owned + 5000),
    )
    assert len(bot.requests) == 2
    assert bot.requests[0].reply_markup is None
    assert bot.requests[1].reply_markup == keyboard


@pytest.mark.asyncio
async def test_photo_caption_boundary_counts_utf16_and_fixed_never_reposts() -> None:
    bot = RecordingBot()
    await send_response(bot, message(), "🦊" * 512, photo=b"image", fixed=True)
    assert isinstance(bot.requests[0], SendPhoto)
    with pytest.raises(ResponseLimitError):
        await send_response(bot, message(), "🦊" * 513, photo=b"image", fixed=True)
    assert len(bot.requests) == 1


@pytest.mark.parametrize("media_kind", ["photo", "video", "audio", "document", "animation"])
@pytest.mark.asyncio
async def test_rich_media_and_text_are_one_exact_native_method(media_kind: str) -> None:
    bot = RecordingBot()
    await send_response(bot, message(), Text(Bold("caption")), **{media_kind: b"media"})
    assert isinstance(bot.requests[0], SendRichMessage)
    assert len(bot.requests) == 1
    assert bot.requests[0].rich_message.skip_entity_detection is True
    assert list(bot.recording.uploads[0].values()) == [b"media"]


@pytest.mark.asyncio
async def test_rich_unsupported_entity_uses_native_caption_without_retry() -> None:
    bot = RecordingBot()
    await send_response(
        bot,
        message(),
        "example",
        photo=b"image",
        entities=[MessageEntity(type="pre", offset=0, length=7, language="python")],
    )
    assert isinstance(bot.requests[0], SendPhoto)
    assert bot.requests[0].caption_entities[0].language == "python"


@pytest.mark.asyncio
async def test_unknown_rich_transport_failure_is_not_retried_as_native() -> None:
    bot = RecordingBot(session=RecordingSession([OSError("socket failed after upload")]))
    with pytest.raises(DeliveryError) as failure:
        await send_response(bot, message(), "caption", photo=b"image")
    assert failure.value.uncertain
    assert len(bot.requests) == 1
    assert isinstance(bot.requests[0], SendRichMessage)


@pytest.mark.asyncio
async def test_partial_delivery_reports_confirmed_prefix_and_never_retries() -> None:
    first = message(message_id=55)
    bot = RecordingBot(session=RecordingSession([first, OSError("lost reply")]))
    with pytest.raises(DeliveryError) as failure:
        await send_response(bot, message(), "x" * 5000, policy=ResponsePolicy(rich=False))
    assert failure.value.confirmed == ((-100123, 55),)
    assert failure.value.attempted_part == 1
    assert failure.value.total_parts == 2
    assert failure.value.uncertain
    assert failure.value.progress.confirmed == failure.value.confirmed
    assert failure.value.progress.phase == "failed"
    assert len(bot.requests) == 2


@pytest.mark.asyncio
async def test_received_rejection_is_distinct_from_uncertain_delivery() -> None:
    rejected = TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="not enough rights")
    bot = RecordingBot(session=RecordingSession([rejected]))
    with pytest.raises(DeliveryError) as failure:
        await send_response(bot, message(), "x")
    assert failure.value.reason == "rejected"
    assert not failure.value.uncertain


@pytest.mark.asyncio
async def test_caller_stream_remains_open_and_immutable_snapshot_is_uploaded() -> None:
    source = io.BytesIO(b"contents")
    bot = RecordingBot()
    await send_response(bot, message(), document=source)
    assert not source.closed
    assert source.tell() == 0
    assert bot.recording.uploads[0]["document"] == b"contents"


@pytest.mark.asyncio
async def test_path_snapshot_is_alive_through_send(tmp_path: Path) -> None:
    path = tmp_path / "result.bin"
    path.write_bytes(b"contents")

    async def responder(_bot: object, _method: object) -> Message:
        path.unlink()
        return message(message_id=101)

    bot = RecordingBot(session=RecordingSession(responder=responder))
    await send_response(bot, message(), document=path)
    assert bot.recording.uploads[0]["document"] == b"contents"


@pytest.mark.asyncio
async def test_media_urls_require_explicit_opt_in() -> None:
    bot = RecordingBot()
    with pytest.raises(ResponseError, match="approved"):
        await send_response(bot, message(), photo="https://example.test/a.jpg")
    assert not bot.requests
    await send_response(bot, message(), photo="https://example.test/a.jpg", allow_remote_media=True)
    assert isinstance(bot.requests[0], SendPhoto)
    assert not bot.recording.uploads[0]


@pytest.mark.asyncio
async def test_caption_edit_uses_actual_media_kind_even_without_existing_caption() -> None:
    bot = RecordingBot()
    photo_message = message(text=None, photo=[PhotoSize(file_id="photo-id", file_unique_id="u", width=10, height=10)])
    await edit_response(bot, photo_message, "caption")
    assert isinstance(bot.requests[0], EditMessageCaption)
    assert bot.requests[0].caption == "caption"
    with pytest.raises(ResponseLimitError):
        await edit_response(bot, photo_message, "x" * 1025)
    assert len(bot.requests) == 1


@pytest.mark.asyncio
async def test_edit_never_copies_stale_keyboard_and_cannot_change_kind() -> None:
    bot = RecordingBot()
    target = message(reply_markup=markup())
    await edit_response(bot, target, "updated")
    method = bot.requests[0]
    assert isinstance(method, EditMessageText)
    assert method.reply_markup is None
    with pytest.raises(ResponseError, match="kind"):
        await edit_response(bot, target, "caption", photo=b"image")
    assert len(bot.requests) == 1


@pytest.mark.asyncio
async def test_native_inline_targets_require_no_fabricated_chat() -> None:
    bot = RecordingBot()
    target = DeliveryTarget(inline_message_id="inline-identity", kind="text")
    assert await edit_response(bot, target, "edited") is True
    method = bot.requests[0]
    assert isinstance(method, EditMessageText)
    assert method.chat_id is None and method.message_id is None
    assert method.inline_message_id == "inline-identity"
    with pytest.raises(ResponseError, match="no known chat"):
        await send_response(bot, target, "new message")


@pytest.mark.asyncio
async def test_inaccessible_callback_never_guesses_reply_topic_or_business_scope() -> None:
    bot = RecordingBot()
    event = query(message=InaccessibleMessage(chat=Chat(id=-100123, type="supergroup"), message_id=10, date=0))
    ctx = CallbackContext(bot, event)
    with pytest.raises(ResponseError, match="explicit DeliveryTarget"):
        await ctx.reply("new message")
    assert not bot.requests
    await ctx.reply(
        "explicit reply", to=DeliveryTarget(chat_id=-100123, thread_id=17, business_connection_id="known-business")
    )
    assert bot.requests[0].message_thread_id == 17
    assert bot.requests[0].business_connection_id == "known-business"


@pytest.mark.asyncio
async def test_inline_media_edit_allows_file_id_but_not_new_upload() -> None:
    bot = RecordingBot()
    target = DeliveryTarget(inline_message_id="inline-identity", kind="photo")
    with pytest.raises(ResponseError, match="upload"):
        await edit_response(bot, target, photo=b"image")
    await edit_response(bot, target, "caption", photo="telegram-file-id")
    assert isinstance(bot.requests[0], EditMessageMedia)


@pytest.mark.asyncio
async def test_rich_edit_requires_complete_explicit_content_and_preserves_blocks() -> None:
    bot = RecordingBot()
    target = DeliveryTarget(chat_id=1, message_id=10, kind="rich")
    with pytest.raises(ResponseError, match="complete"):
        await edit_response(bot, target, "a partial paragraph")
    rich = InputRichMessage(
        blocks=[
            InputRichBlockParagraph(text="entire replacement"),
            InputRichBlockPhoto(photo=InputMediaPhoto(media="photo-id")),
        ],
        skip_entity_detection=True,
    )
    await edit_response(bot, target, rich_message=rich)
    method = bot.requests[0]
    assert isinstance(method, EditMessageText)
    assert method.text is None and method.rich_message is not None
    assert method.rich_message.blocks[0].text == "entire replacement"
    assert method.rich_message.blocks[1].photo.media == "photo-id"
    assert method.rich_message.blocks[1].photo.parse_mode is None
    assert method.reply_markup is None
    assert rich.blocks and len(rich.blocks) == 2


@pytest.mark.asyncio
async def test_rich_inline_edit_cannot_smuggle_url_upload_inside_block() -> None:
    bot = RecordingBot()
    rich = InputRichMessage(blocks=[InputRichBlockPhoto(photo=InputMediaPhoto(media="https://example.test/photo.jpg"))])
    with pytest.raises(ResponseError, match="Inline rich"):
        await edit_response(
            bot, DeliveryTarget(inline_message_id="inline", kind="rich"), rich_message=rich, allow_remote_media=True
        )
    assert bot.requests == []


@pytest.mark.asyncio
async def test_rich_edit_enforces_total_text_budget_before_writing() -> None:
    bot = RecordingBot()
    rich = InputRichMessage(blocks=[InputRichBlockParagraph(text="x" * 32769)])
    with pytest.raises(ResponseLimitError):
        await edit_response(bot, DeliveryTarget(chat_id=1, message_id=10, kind="rich"), rich_message=rich)
    assert bot.requests == []


@pytest.mark.asyncio
async def test_recorded_media_response_can_be_used_as_an_edit_target() -> None:
    bot = RecordingBot()
    sent = await send_response(bot, message(), "first caption", photo=b"image", fixed=True)
    assert isinstance(sent, Message) and sent.photo
    await edit_response(bot, sent, "new caption")
    assert isinstance(bot.requests[1], EditMessageCaption)


@pytest.mark.asyncio
async def test_cancelled_file_preparation_joins_before_managed_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    path = tmp_path / "source.bin"
    path.write_bytes(b"source")

    def read_while_owned(file: Path, _limit: int) -> bytes:
        started.set()
        assert release.wait(5)
        try:
            return file.read_bytes()
        finally:
            finished.set()

    monkeypatch.setattr("teleforge.delivery._read_path", read_while_owned)
    bot = RecordingBot()
    task = asyncio.create_task(send_response(bot, message(), document=path))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert not bot.requests


@pytest.mark.asyncio
async def test_callback_actor_ui_and_acquired_input_are_separate() -> None:
    bot = RecordingBot()
    event = query()
    ctx = context_for(bot, event)
    assert isinstance(ctx, CallbackContext)
    assert ctx.user and ctx.user.id == 88
    assert ctx.message and ctx.message.from_user.id == 42
    assert ctx.input_sources == {}
    source = message(message_id=90)
    ctx.input_sources["text"] = source
    ctx.response_target = source
    await ctx.reply("reply")
    await ctx.edit("card")
    assert bot.requests[0].reply_parameters.message_id == 90
    assert bot.requests[1].message_id == 10


@pytest.mark.asyncio
async def test_context_effects_survive_later_planning_error() -> None:
    bot = RecordingBot()
    ctx = Context(bot, message())
    await ctx.reply("committed UI")
    with pytest.raises(ResponseLimitError):
        await ctx.reply("x" * 4097, fixed=True)
    assert ctx.has_effects
    assert len(bot.requests) == 1


@pytest.mark.asyncio
async def test_callback_acknowledgement_has_one_owner() -> None:
    bot = RecordingBot()
    ctx = CallbackContext(bot, query())
    assert await ctx.answer("validation", show_alert=True) is True
    assert await ctx.answer("ignored") is None
    await ctx.finish()
    assert len(bot.requests) == 1
    assert ctx.acknowledgement.confirmed
    assert bot.requests[0].text == "validation"


@pytest.mark.asyncio
async def test_native_acknowledgement_optout_disables_finish() -> None:
    bot = RecordingBot()
    ctx = CallbackContext(bot, query())
    native = ctx.manual_ack()
    await bot(native.answer("native"))
    await ctx.finish()
    assert len(bot.requests) == 1


@pytest.mark.asyncio
async def test_uncertain_acknowledgement_is_never_retried() -> None:
    bot = RecordingBot(session=RecordingSession([OSError("lost acknowledgement")]))
    ctx = CallbackContext(bot, query())
    with pytest.raises(DeliveryError):
        await ctx.answer()
    await ctx.finish()
    assert len(bot.requests) == 1
    assert ctx.acknowledgement.attempted and ctx.acknowledgement.uncertain


@pytest.mark.asyncio
async def test_context_does_not_answer_while_primary_exception_unwinds() -> None:
    primary = RuntimeError("domain failure")

    class BrokenButton(CallbackData, prefix="broken"):
        pass

    class BrokenFeature(Feature, key="test.delivery-error"):
        @callback(BrokenButton)
        async def broken(self, ctx: CallbackContext) -> None:
            raise primary

    # Any fallback acknowledgement would fail too and must not mask the domain error.
    bot = RecordingBot(session=RecordingSession([OSError("acknowledgement failed")]))
    async with App(BrokenFeature()) as app:
        with pytest.raises(RuntimeError) as failure:
            await app.feed_update(bot, Update(update_id=1, callback_query=query(data=BrokenButton().pack())))
    assert failure.value is primary
    assert not bot.requests


@pytest.mark.asyncio
async def test_cancelled_write_exposes_uncertainty_and_preserves_cancellation() -> None:
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def responder(_bot: object, _method: object) -> bool:
        entered.set()
        await hold.wait()
        return True

    bot = RecordingBot(session=RecordingSession(responder=responder))
    ctx = Context(bot, message())
    task = asyncio.create_task(ctx.reply("hello"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.delivery_progress and ctx.delivery_progress.uncertain
    assert ctx.delivery_progress.phase == "cancelled"
    assert len(bot.requests) == 1

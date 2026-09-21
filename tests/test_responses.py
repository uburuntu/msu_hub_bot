"""Command responses use real aiogram methods with a synthetic, offline transport."""

import asyncio
import io
import json
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import ClientDecodeError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import CopyMessages, ForwardMessages, SendMediaGroup
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichBlockPhoto,
    Message,
    MessageEntity,
    URLInputFile,
)
from aiogram.utils.formatting import Bold, Pre, Text, TextLink
from PIL import Image

from msu_hub_bot.telegram import responses
from msu_hub_bot.telegram.response_text import TextNeedsFile, format_text, rich_text, split_text, units
from msu_hub_bot.telegram.responses import (
    ResponseDeliveryError,
    ResponseError,
    ResponseLimitError,
    ResponsePolicy,
    ResponseProgress,
    send_response,
)
from msu_hub_bot.telegram.wrapper import BotWrapper
from telegram_helpers import RecordingSession, make_message


class NumberedSession(RecordingSession):
    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        await asyncio.sleep(0)
        return make_message(bot, message_id=100 + len(self.methods), chat={"id": method.chat_id, "type": "supergroup"})


@pytest.fixture
def rig():
    session = NumberedSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    return bot, session, make_message(bot, message_id=71)


def visible(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(map(visible, value))
    if hasattr(value, "alternative_text"):
        return value.alternative_text
    return visible(value.text)


def rich_body(method):
    return "".join(visible(block.text) for block in method.rich_message.blocks if hasattr(block, "text"))


async def test_plain_text_is_literal_despite_global_html_and_always_returns_list(rig):
    _, session, target = rig
    result = await send_response(target, '<b>literal & "text"</b>')
    assert isinstance(result, list) and len(result) == 1
    method = session.methods[0]
    assert method.text == '<b>literal & "text"</b>' and method.parse_mode is None
    assert method.link_preview_options.is_disabled


async def test_fixed_has_native_message_type_and_never_chooses_rich(rig):
    _, session, target = rig
    result = await send_response(target, Bold("Caption"), photo=b"image", fixed=True)
    assert isinstance(result, Message) and result.message_id == 101
    method = session.methods[0]
    assert method.__api_method__ == "sendPhoto" and method.caption == "Caption"
    assert method.caption_entities[0].type == "bold" and method.parse_mode is None


@pytest.mark.parametrize("text,media", [("x" * 4097, {}), ("😀" * 513, {"photo": b"image"})])
async def test_fixed_overflow_rejects_before_any_mutation(rig, text, media):
    _, session, target = rig
    with pytest.raises(ResponseLimitError):
        await send_response(target, text, fixed=True, **media)
    assert session.methods == []


async def test_rich_preference_preserves_image_and_native_formatting(rig):
    bot, session, target = rig
    content = Text(Bold("Привет 😀"), " ", TextLink("источник", url="https://example.test/post"))
    result = await send_response(target, content, photo=b"image")
    assert len(result) == 1
    method = session.methods[0]
    assert method.__api_method__ == "sendRichMessage"
    assert rich_body(method) == "Привет 😀 источник"
    assert method.rich_message.blocks[-1].photo.media.data == b"image"
    assert method.rich_message.skip_entity_detection is True
    assert method.rich_message.blocks[0].text[0].type == "bold"
    uploads = {}
    serialized = json.loads(session.prepare_value(method.rich_message, bot=bot, files=uploads))
    photo = serialized["blocks"][1]["photo"]["media"]
    assert photo.startswith("attach://") and uploads[photo.removeprefix("attach://")].data == b"image"


async def test_rich_splits_large_text_without_repeating_media_or_losing_characters(rig):
    _, session, target = rig
    text = "😀 paragraph\n" * 4000
    await send_response(target, text, photo=b"image")
    assert len(session.methods) == 2
    assert "".join(rich_body(method) for method in session.methods) == text
    assert sum(isinstance(block, InputRichBlockPhoto) for method in session.methods for block in method.rich_message.blocks) == 1
    assert all(units(rich_body(method)) <= 32768 for method in session.methods)


async def test_native_policy_long_caption_uses_photo_then_complete_text_with_one_keyboard(rig):
    _, session, target = rig
    text = "abc 😀 " * 900
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Link", url="https://example.test")]])
    await send_response(target, text, photo=b"image", policy=ResponsePolicy(rich=False), reply_markup=keyboard)
    assert session.methods[0].__api_method__ == "sendPhoto" and session.methods[0].caption is None
    assert "".join(method.text for method in session.methods[1:]) == text
    assert all(method.reply_markup is None for method in session.methods[:-1])
    assert session.methods[-1].reply_markup == keyboard
    assert [method.reply_parameters.message_id for method in session.methods] == [71, 101, 102]


@pytest.mark.parametrize("length,methods", [(8000, ["sendPhoto", "sendMessage", "sendMessage"]), (10000, ["sendPhoto", "sendDocument"])])
async def test_soft_budget_counts_separate_media_before_choosing_complete_file(rig, length, methods):
    _, session, target = rig
    text = "x" * length
    await send_response(target, text, photo=b"photo", policy=ResponsePolicy(rich=False))
    assert [method.__api_method__ for method in session.methods] == methods
    if methods[-1] == "sendDocument":
        assert session.methods[-1].document.data.decode() == text
    else:
        assert "".join(method.text for method in session.methods[1:]) == text


async def test_unsupported_rich_entity_uses_native_caption_without_losing_language(rig):
    _, session, target = rig
    await send_response(target, Pre("print('literal')", language="python"), photo=b"image")
    method = session.methods[0]
    assert method.__api_method__ == "sendPhoto"
    assert method.caption_entities[0].language == "python"


@pytest.mark.parametrize("with_photo,rich", [(False, False), (True, False), (True, True)])
async def test_soft_limit_chooses_complete_file_before_any_text_send_and_keeps_media(rig, with_photo, rich):
    _, session, target = rig
    text = "😀 " * (40000 if rich else 10000)
    await send_response(target, text, photo=b"image" if with_photo else None, policy=ResponsePolicy(rich=rich, soft_messages=1))
    assert [method.__api_method__ for method in session.methods] == (["sendPhoto"] if with_photo else []) + ["sendDocument"]
    assert session.methods[-1].document.data.decode() == text
    assert session.methods[-1].document.filename == "result.txt"


async def test_file_overflow_keeps_hidden_link_destination(rig):
    _, session, target = rig
    text = TextLink("x" * 10000, url="https://example.test/full-target")
    await send_response(target, text, policy=ResponsePolicy(soft_messages=1))
    payload = session.methods[0].document.data.decode()
    assert payload.startswith("x" * 10000) and payload.endswith("https://example.test/full-target")


@pytest.mark.parametrize("content", ["😀" * 30, TextLink("x", url="https://example.test/" + "x" * 200)])
async def test_hard_limit_rejects_before_sending_even_if_media_fits(rig, content):
    _, session, target = rig
    with pytest.raises(ResponseLimitError):
        await send_response(target, content, photo=b"image", policy=ResponsePolicy(max_output_bytes=100))
    assert session.methods == []


async def test_combined_media_and_text_budget_is_checked_before_publication(rig):
    _, session, target = rig
    with pytest.raises(ResponseLimitError):
        await send_response(target, "x" * 60, document=b"x" * 60, policy=ResponsePolicy(max_output_bytes=100))
    assert session.methods == []


async def test_reply_context_survives_chaining(rig):
    bot, session, _ = rig
    target = make_message(
        bot,
        message_id=71,
        message_thread_id=88,
        is_topic_message=True,
        business_connection_id="business",
        direct_messages_topic={"topic_id": 123},
    )
    await send_response(target, "x" * 5000, allow_sending_without_reply=True)
    assert [method.reply_parameters.message_id for method in session.methods] == [71, 101]
    assert all(
        method.message_thread_id == 88 and method.business_connection_id == "business" and method.direct_messages_topic_id == 123
        for method in session.methods
    )
    assert all(method.reply_parameters.allow_sending_without_reply for method in session.methods)


async def test_general_topic_and_unsupported_ephemeral_context(rig):
    bot, session, target = rig
    await send_response(target, "hello")
    assert session.methods[0].message_thread_id is None
    with pytest.raises(ResponseError):
        await send_response(make_message(bot, message_id=0, ephemeral_message_id=7), "hello")
    assert len(session.methods) == 1


async def test_fixed_video_preserves_geometry_and_streaming(rig):
    _, session, target = rig
    await send_response(target, "video", video=b"video", width=1920, height=1080, duration=12, supports_streaming=True, fixed=True)
    method = session.methods[0]
    assert (method.width, method.height, method.duration, method.supports_streaming) == (1920, 1080, 12, True)
    assert method.parse_mode is None


async def test_bare_media_does_not_invent_empty_caption_or_entities(rig):
    _, session, target = rig
    await send_response(target, photo=b"photo", fixed=True)
    assert session.methods[0].caption is None and session.methods[0].caption_entities is None


async def test_borrowed_path_and_buffer_are_snapshotted_without_closing_caller_resources(rig, tmp_path):
    _, session, target = rig
    path = tmp_path / "report.bin"
    path.write_bytes(b"file contents")
    await send_response(target, document=FSInputFile(path), fixed=True)
    assert path.read_bytes() == b"file contents"
    assert session.methods[0].document.data == b"file contents"
    assert session.methods[0].document.filename == "report.bin"
    with io.BytesIO(b"whole stream") as stream:
        stream.seek(5)
        await send_response(target, document=stream, fixed=True)
        assert not stream.closed and stream.tell() == 5
        assert session.methods[1].document.data == b"whole stream"


async def test_pil_photo_encoding_does_not_close_caller_image(rig):
    _, session, target = rig
    with Image.new("RGB", (16, 12), "red") as source:
        await send_response(target, photo=source, fixed=True)
        assert source.getpixel((0, 0)) == (255, 0, 0)
    with Image.open(io.BytesIO(session.methods[0].photo.data)) as output:
        assert output.size == (16, 12)


async def test_cancelled_preparation_finishes_before_borrowed_resource_scope_ends(rig, tmp_path, monkeypatch):
    _, session, target = rig
    path = tmp_path / "borrowed.bin"
    path.write_bytes(b"prepared")
    entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = responses._read_path

    def read_path(path, limit):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        try:
            return original(path, limit)
        finally:
            finished.set()

    monkeypatch.setattr(responses, "_read_path", read_path)
    task = asyncio.create_task(send_response(target, document=path))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # Repeated cancellation must not release the borrowed scope.
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and session.methods == []
    path.unlink()


@pytest.mark.parametrize("worker_cancelled", [False, True])
def test_shutdown_cancellation_joins_preparation_before_closing_borrowed_stream(worker_cancelled):
    # A cancelled worker Task made the cancellation join spin without yielding.
    # Isolate that regression so its timeout cannot hang the pytest event loop.
    program = """
import asyncio
import io
import sys
import threading
from contextvars import ContextVar

sys.path.insert(0, sys.argv[1])
from msu_hub_bot.telegram.responses import _prepare_bytes

context = ContextVar("preparation_context")

async def main():
    context.set("caller")
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    stream = io.BytesIO(b"synthetic")
    read = []

    def prepare():
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(2)
            assert context.get() == "caller"
            read.append(stream.getvalue())
            if sys.argv[2] == "True":
                raise asyncio.CancelledError
            return read[-1]
        finally:
            finished.set()

    async def deliver():
        with stream:
            try:
                await _prepare_bytes(prepare)
            finally:
                assert finished.is_set()

    task = asyncio.create_task(deliver())
    await asyncio.wait_for(entered.wait(), 2)
    # asyncio.Runner shutdown also cancels all pending Tasks before gathering.
    pending = asyncio.all_tasks() - {asyncio.current_task()}
    for pending_task in pending:
        pending_task.cancel()
    release.set()
    await asyncio.gather(*pending, return_exceptions=True)
    assert task.cancelled()
    assert stream.closed and finished.is_set() and read == [b"synthetic"]

asyncio.run(main())
"""
    subprocess.run(
        [sys.executable, "-c", program, str(Path(responses.__file__).resolve().parents[2]), str(worker_cancelled)],
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
    )


@pytest.mark.parametrize("source", ["https://example.test/image", URLInputFile("https://example.test/image")])
async def test_lazy_network_media_is_rejected_before_transport(rig, source):
    _, session, target = rig
    with pytest.raises(ResponseError):
        await send_response(target, photo=source)
    assert session.methods == []


async def test_explicit_native_remote_media_preserves_url_and_request_timeout(rig, monkeypatch):
    _, session, target = rig
    original = session.make_request
    timeouts = []

    async def request(bot, method, timeout=None):
        timeouts.append(timeout)
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", request)
    await send_response(
        target,
        "caption",
        photo="https://upload.wikimedia.org/approved.jpg",
        allow_remote_media=True,
        request_timeout=15,
        fixed=True,
    )
    assert session.methods[0].photo == "https://upload.wikimedia.org/approved.jpg"
    assert timeouts == [15]


@pytest.mark.parametrize(
    "source", ["file:///private/file", "https://user:password@example.test/photo", URLInputFile("https://example.test")]
)
async def test_remote_opt_in_does_not_allow_local_fetch_streams_or_non_web_urls(rig, source):
    _, session, target = rig
    with pytest.raises(ResponseError):
        await send_response(target, photo=source, allow_remote_media=True, fixed=True)
    assert session.methods == []


async def test_oversize_path_and_invalid_combination_fail_without_sends(rig, tmp_path):
    _, session, target = rig
    path = tmp_path / "large"
    path.write_bytes(b"x" * 101)
    with pytest.raises(ResponseLimitError):
        await send_response(target, document=path, policy=ResponsePolicy(max_output_bytes=100))
    with pytest.raises(ResponseError):
        await send_response(target, photo=b"photo", video=b"video")
    assert session.methods == []


async def test_shared_wrapper_lane_prevents_interleaving_with_legacy_composite_sender(rig):
    bot, session, target = rig
    await asyncio.gather(
        send_response(target, "n" * 12000, policy=ResponsePolicy(soft_messages=4)),
        bot.send_super_message("o" * 12000, None, None, None, target.chat.id),
    )
    assert [method.text[0] for method in session.methods] == ["n"] * 3 + ["o"] * 3
    assert not bot._chat_sends


@pytest.mark.parametrize("failure", ["network", "decode", "rejected"])
async def test_partial_failure_preserves_confirmed_prefix_without_replay_or_raw_cause_export(failure):
    class FailSecond(NumberedSession):
        async def make_request(self, bot, method, timeout=None):
            if self.methods:
                self.methods.append(method)
                if failure == "decode":
                    raise ClientDecodeError("malformed", ValueError("private"), {"content": "SYNTHETIC_PRIVATE_BODY"})
                cls = TelegramBadRequest if failure == "rejected" else TelegramNetworkError
                raise cls(method=method, message="SYNTHETIC_PRIVATE_BODY")
            return await super().make_request(bot, method, timeout)

    session = FailSecond()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    target = make_message(bot)
    with pytest.raises(ResponseDeliveryError) as caught:
        await send_response(target, "x" * 11000)
    error = caught.value
    assert error.confirmed == ((target.chat.id, 101),)
    assert (error.attempted_part, error.total_parts, error.uncertain) == (1, 3, failure != "rejected")
    assert len(session.methods) == 2 and not bot._chat_sends
    assert "SYNTHETIC_PRIVATE_BODY" not in "".join(traceback.format_exception(error))


async def test_rich_rejection_does_not_replay_media_through_an_alternate_layout(rig, monkeypatch):
    _, session, target = rig

    async def reject(bot, method, timeout=None):
        session.methods.append(method)
        raise TelegramBadRequest(method=method, message="rejected")

    monkeypatch.setattr(session, "make_request", reject)
    with pytest.raises(ResponseDeliveryError) as caught:
        await send_response(target, "full text", photo=b"image")
    assert not caught.value.uncertain and caught.value.confirmed == ()
    assert [method.__api_method__ for method in session.methods] == ["sendRichMessage"]


async def test_zero_message_id_cannot_confirm_publication_or_chain_tail(rig, monkeypatch):
    _, session, target = rig

    async def unconfirmed(bot, method, timeout=None):
        session.methods.append(method)
        return make_message(bot, message_id=0)

    monkeypatch.setattr(session, "make_request", unconfirmed)
    with pytest.raises(ResponseDeliveryError) as caught:
        await send_response(target, "x" * 5000)
    assert caught.value.uncertain and caught.value.confirmed == () and caught.value.attempted_part == 0
    assert len(session.methods) == 1


async def test_cancelled_send_keeps_confirmed_prefix_and_never_sends_tail():
    entered = asyncio.Event()

    class HoldSecond(NumberedSession):
        async def make_request(self, bot, method, timeout=None):
            if self.methods:
                self.methods.append(method)
                entered.set()
                await asyncio.Event().wait()
            return await super().make_request(bot, method, timeout)

    session = HoldSecond()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    target = make_message(bot)
    progress = ResponseProgress()
    task = asyncio.create_task(send_response(target, "x" * 11000, progress=progress))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert progress.phase == "cancelled" and progress.uncertain
    assert progress.confirmed == ((target.chat.id, 101),)
    assert len(session.methods) == 2 and not bot._chat_sends


async def test_timeout_waiting_for_lane_does_not_claim_a_mutation(rig):
    bot, session, target = rig
    progress = ResponseProgress()
    async with bot.serial_send(target.chat.id):
        with pytest.raises(ResponseDeliveryError) as caught:
            await send_response(target, "hello", policy=ResponsePolicy(timeout=0.01), progress=progress)
    assert not caught.value.uncertain and caught.value.attempted_part is None
    assert caught.value.reason == "not_attempted"
    assert session.methods == [] and not bot._chat_sends


async def test_timeout_after_transport_entry_preserves_unknown_attempt_without_retry(rig, monkeypatch):
    bot, session, target = rig

    async def hold(bot, method, timeout=None):
        session.methods.append(method)
        await asyncio.Event().wait()

    monkeypatch.setattr(session, "make_request", hold)
    with pytest.raises(ResponseDeliveryError) as caught:
        await send_response(target, "x" * 5000, policy=ResponsePolicy(timeout=0.01))
    assert caught.value.uncertain and caught.value.attempted_part == 0 and caught.value.total_parts == 2
    assert len(session.methods) == 1 and not bot._chat_sends


async def test_plain_bot_lane_is_released_after_success():
    bot = Bot("123456789:" + "a" * 35, session=NumberedSession())
    await send_response(make_message(bot), "hello")
    assert bot not in responses._plain_lanes


@pytest.mark.parametrize(
    "method",
    [
        SendMediaGroup(chat_id=1, media=[InputMediaPhoto(media="one"), InputMediaPhoto(media="two")]),
        CopyMessages(chat_id=1, from_chat_id=2, message_ids=[1, 2]),
        ForwardMessages(chat_id=1, from_chat_id=2, message_ids=[1, 2]),
    ],
)
async def test_multisend_retry_after_is_never_replayed(method):
    class Rejected(RecordingSession):
        async def make_request(self, bot, attempted, timeout=None):
            self.methods.append(attempted)
            raise TelegramRetryAfter(method=attempted, message="wait", retry_after=0)

    session = Rejected()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    with pytest.raises(TelegramRetryAfter):
        await bot(method)
    assert len(session.methods) == 1


async def test_send_permission_error_does_not_leave_chat(rig, monkeypatch):
    bot, session, target = rig

    async def reject(bot, method, timeout=None):
        session.methods.append(method)
        raise TelegramBadRequest(method=method, message="Bad Request: have no rights to send a message")

    monkeypatch.setattr(session, "make_request", reject)
    with pytest.raises(TelegramBadRequest):
        await bot.send_message(target.chat.id, "hello")
    assert [method.__api_method__ for method in session.methods] == ["sendMessage"]


def test_entity_splitting_reconstructs_nested_non_bmp_text_and_complete_link_targets():
    original = Text("😀", Bold("α " * 2400, TextLink("𐍈" * 1700, url="https://example.test/full")), " tail")
    value = format_text(original, None, 100000)
    parts = list(split_text(value))
    assert "".join(part.text for part in parts) == value.text
    for part in parts:
        assert part.fits(4096)
        encoded = part.text.encode("utf-16-le")
        for entity in part.entities:
            fragment = encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
            assert fragment
            if entity.type == "text_link":
                assert entity.url == "https://example.test/full"


def test_atomic_custom_emoji_moves_whole_to_next_chunk():
    text = "x" * 4095 + "😀" + "end"
    value = format_text(text, [MessageEntity(type="custom_emoji", offset=4095, length=2, custom_emoji_id="123")], 100000)
    parts = list(split_text(value))
    assert [part.text for part in parts] == ["x" * 4095, "😀end"]
    assert parts[1].entities[0].offset == 0 and parts[1].entities[0].length == 2


def test_entity_count_reduction_never_cuts_an_indivisible_parent():
    value = format_text(
        "prefix" + "https://example.test" + "tail",
        [MessageEntity(type="url", offset=6, length=20), MessageEntity(type="bold", offset=9, length=3)],
        10000,
    )
    parts = split_text(value, limit=100, max_entities=1)
    assert next(parts).text == "prefix"
    with pytest.raises(TextNeedsFile):
        next(parts)


def test_formatting_work_limits_count_empty_leaf_nodes_before_rendering():
    with pytest.raises(ResponseLimitError):
        format_text(Text(*([""] * 10001)), None, 100000)


async def test_text_and_entity_metadata_share_the_hard_budget(rig):
    _, session, target = rig
    value = TextLink("x" * 100, url="https://example.test/" + "y" * 100)
    with pytest.raises(ResponseLimitError):
        await send_response(target, value, policy=ResponsePolicy(max_output_bytes=250))
    assert session.methods == []


async def test_rich_cannot_drop_style_inside_custom_emoji(rig):
    _, session, target = rig
    entities = [
        MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="123"),
        MessageEntity(type="bold", offset=0, length=2),
    ]
    await send_response(target, "😀", entities=entities, photo=b"image")
    assert session.methods[0].__api_method__ == "sendPhoto"
    assert session.methods[0].caption_entities == entities


@pytest.mark.parametrize(
    "entity",
    [
        MessageEntity(type="bold", offset=1, length=1),
        MessageEntity(type="bold", offset=-1, length=1),
        MessageEntity(type="bold", offset=0, length=99),
    ],
)
def test_invalid_utf16_entity_boundaries_are_rejected(entity):
    with pytest.raises(ResponseError):
        format_text("😀hello", [entity], 1000)


def test_rich_native_formatting_uses_utf16_not_aiogram_text_slicing():
    value = format_text(Text("😀", Bold("ab")), None, 1000)
    assert visible(rich_text(value)) == "😀ab"
    parts = list(split_text(value, limit=2))
    assert [part.text for part in parts] == ["😀", "ab"]
    assert parts[1].entities[0].offset == 0 and parts[1].entities[0].length == 2


async def test_whitespace_only_chunk_uses_lossless_file_instead_of_dropping_tail(rig):
    _, session, target = rig
    text = "\n" * 6000 + "visible tail"
    await send_response(target, text)
    assert len(session.methods) == 1 and session.methods[0].document.data.decode() == text

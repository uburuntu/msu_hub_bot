"""Offline tests: no bot token, Telegram connection or sticker mutations."""

import asyncio
import gzip
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetStickerSet
from aiogram.fsm.storage.base import StorageKey

from common.tg.state import ReleasableEventIsolation, UpdateStateContext
from hub_bot.utils import sticker_media as media


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def handlers():
    # Each test owns its provider and worker stubs; application state is never imported.
    source = (ROOT / "hub_bot/commands/sticker.py").read_text()
    ns = {"__name__": "synthetic_sticker"}
    exec(compile(source, "<sticker>", "exec"), ns)

    async def download(file, bot):
        return io.BytesIO(file.data)

    async def execute(function, *args):
        return function(*args), False

    ns["download"] = download
    ns["cpu_executor"] = SimpleNamespace(run=AsyncMock(side_effect=execute))
    original = ns["process_sticker_chat"]

    async def process_sticker_chat(message, meta, state):
        return await original(message, meta, state, message.bot, ns["cpu_executor"])

    ns["process_sticker_chat"] = process_sticker_chat
    deletion = ns["process_sticker_delete"]

    async def process_sticker_delete(message):
        return await deletion(message, message.bot)

    ns["process_sticker_delete"] = process_sticker_delete
    ns["extract_image"] = AsyncMock(return_value=(None, None))
    # The extractor itself stays real in integration tests.
    ns["Extractor"] = SimpleNamespace(image=ns["extract_image"])

    async def finish_chat_set(message, state, data):
        data.setdefault("sticker_origin_message_id", message.message_id)
        data.setdefault("sticker_upload", {})
        state.key = StorageKey(bot_id=1, chat_id=message.chat.id, user_id=message.from_user.id)
        state.get_data = AsyncMock(return_value=data)
        state.get_state = AsyncMock(return_value=ns["StickerStates"].sticker_set_name.state)
        return await ns["Stickers"].finish_chat_set(
            message, state, data, message.bot, UpdateStateContext(eligible=True), ReleasableEventIsolation()
        )

    ns["finish_chat_set"] = finish_chat_set
    return ns


def error(text, *, network=False):
    return (TelegramNetworkError if network else TelegramBadRequest)(method=GetStickerSet(name="pack"), message=text)


@pytest.mark.parametrize("duration,accelerated", [(0.1, False), (3, False), (3.001, True), (6, True), (7, True), (7.001, True), (60, True)])
def test_speed_boundaries(duration, accelerated):
    speed = media.speed_factor(duration)
    assert (speed > 1) == accelerated
    assert min(duration, 7) / speed <= 3


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_invalid_duration(duration):
    with pytest.raises(media.StickerMediaError):
        media.speed_factor(duration)


@pytest.mark.parametrize("size", [(640, 480), (480, 640), (1, 1024)])
def test_static_dimensions(size):
    source = io.BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 128)).save(source, format="PNG")
    prepared = media.prepare_media(source.getvalue(), "static")
    image = Image.open(io.BytesIO(prepared.payload))
    assert prepared.kind == "static" and not prepared.trimmed
    assert max(image.size) == 512 and min(image.size) > 0
    assert image.getpixel((0, 0))[3] == 128


def test_tgs_roundtrip():
    data = gzip.compress(json.dumps(dict(ip=0, op=180, fr=60)).encode())
    assert media.prepare_media(data, "animated") == media.PreparedMedia("animated", data)


def test_invalid_long_tgs_is_not_retimed_as_video():
    data = gzip.compress(json.dumps(dict(ip=0, op=480, fr=60)).encode())
    with pytest.raises(media.StickerMediaError, match="3 секунд"):
        media.prepare_tgs(data)


@pytest.mark.parametrize("data", [b"invalid", gzip.compress(b"{}")])
def test_bad_tgs(data):
    with pytest.raises(media.StickerMediaError):
        media.prepare_tgs(data)


def test_invalid_duration_rejected_before_encoding(monkeypatch):
    monkeypatch.setattr(media, "_probe", lambda path: ({}, {"codec_name": "h264"}, 0))
    calls = []
    monkeypatch.setattr(media, "_run", lambda command: calls.append(command))
    with pytest.raises(media.StickerMediaError, match="длительность"):
        media.prepare_video(b"video")
    assert not calls


@pytest.mark.parametrize("duration", [6, 7, 7.001, 60])
def test_video_conversion_command(monkeypatch, duration):
    probes = iter(
        [
            ({}, {"codec_name": "h264"}, duration),
            ({"streams": [{"codec_type": "video"}]}, {"codec_name": "vp9", "width": 512, "height": 288, "avg_frame_rate": "30/1"}, 2.967),
        ]
    )
    monkeypatch.setattr(media, "_probe", lambda path: next(probes))
    commands = []

    def run(command):
        commands.append(command)
        Path(command[-1]).write_bytes(b"encoded")

    monkeypatch.setattr(media, "_run", run)
    assert media.prepare_video(b"source") == media.PreparedMedia("video", b"encoded", trimmed=duration > 7)
    command = commands[0]
    assert "-an" in command
    if duration > 7:
        assert command.index("-t") < command.index("-i")
        assert command[command.index("-t") + 1] == "7"
    else:
        assert "-t" not in command
    assert f"setpts=(PTS-STARTPTS)/{media.speed_factor(duration):.12f}" in command[command.index("-vf") + 1]


def message(admin=True, kind="static"):
    sticker = SimpleNamespace(
        file_id="input",
        file_size=10,
        data=b"data",
        type="regular",
        is_animated=kind == "animated",
        is_video=kind == "video",
        set_name="with_love_for_100_by_msu_hub_bot",
        delete_from_set=AsyncMock(),
    )
    target = SimpleNamespace(sticker=sticker, animation=None, video=None, video_note=None, photo=[], document=None)
    chat = SimpleNamespace(
        id=-100,
        type="supergroup",
        all_members_are_administrators=False,
        get_administrators=AsyncMock(return_value=[SimpleNamespace(user=SimpleNamespace(id=1 if admin else 2))]),
    )
    return SimpleNamespace(
        chat=chat,
        message_id=1,
        from_user=SimpleNamespace(id=1),
        sender_chat=None,
        reply_to_message=target,
        sticker=None,
        animation=None,
        video=None,
        video_note=None,
        photo=[],
        document=None,
        reply=AsyncMock(),
        reply_sticker=AsyncMock(),
        bot=SimpleNamespace(
            upload_sticker_file=AsyncMock(return_value=SimpleNamespace(file_id="uploaded", file_unique_id="unique-uploaded")),
            add_sticker_to_set=AsyncMock(return_value=True),
            create_new_sticker_set=AsyncMock(return_value=True),
            delete_sticker_from_set=AsyncMock(return_value=True),
            get_chat_administrators=chat.get_administrators,
            get_sticker_set=AsyncMock(return_value=registered_pack(kind)),
            get_file=AsyncMock(return_value=SimpleNamespace(file_unique_id="unique-uploaded")),
        ),
    )


def registered_pack(kind="static"):
    return SimpleNamespace(
        stickers=[
            SimpleNamespace(
                file_id="registered-sticker",
                file_unique_id="unique-uploaded",
                type="regular",
                is_animated=kind == "animated",
                is_video=kind == "video",
            )
        ]
    )


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
def test_source_types(handlers, kind):
    source, actual = handlers["Stickers"].source_media(message(kind=kind))
    assert actual == kind and source.file_id == "input"


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
def test_delete_all_formats(handlers, kind):
    m = message(kind=kind)
    asyncio.run(handlers["process_sticker_delete"](m))
    m.bot.delete_sticker_from_set.assert_awaited_once()


def test_delete_non_admin(handlers):
    m = message(admin=False, kind="video")
    asyncio.run(handlers["process_sticker_delete"](m))
    m.bot.delete_sticker_from_set.assert_not_awaited()


def test_delete_without_set(handlers):
    m = message()
    m.reply_to_message.sticker.set_name = None
    asyncio.run(handlers["process_sticker_delete"](m))
    m.bot.delete_sticker_from_set.assert_not_awaited()


def test_invalid_media_never_uploaded(handlers):
    m = message(kind="video")

    def reject(*args):
        raise media.StickerMediaError("Не удалось определить длительность анимации.")

    handlers["prepare_media"] = reject
    asyncio.run(handlers["process_sticker_chat"](m, None, None))
    m.bot.upload_sticker_file.assert_not_awaited()
    m.bot.add_sticker_to_set.assert_not_awaited()
    m.bot.create_new_sticker_set.assert_not_awaited()
    assert "длительность" in m.reply.call_args.args[0]


def test_non_admin_cannot_add(handlers):
    m = message(admin=False)
    asyncio.run(handlers["process_sticker_chat"](m, None, None))
    m.bot.upload_sticker_file.assert_not_awaited()
    m.bot.add_sticker_to_set.assert_not_awaited()
    m.bot.create_new_sticker_set.assert_not_awaited()


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
def test_upload_and_add_modern_api(handlers, kind):
    m = message(kind=kind)
    handlers["prepare_media"] = lambda data, kind: media.PreparedMedia(kind, b"prepared")
    meta = SimpleNamespace(extract_text=lambda: (m, "😎"))
    asyncio.run(handlers["process_sticker_chat"](m, meta, None))
    handlers["cpu_executor"].run.assert_awaited_once_with(handlers["prepare_media"], b"data", kind)
    m.bot.upload_sticker_file.assert_awaited_once()
    m.bot.add_sticker_to_set.assert_awaited_once()
    added = m.bot.add_sticker_to_set.call_args.kwargs["sticker"]
    assert added.sticker == "uploaded" and added.format == kind and added.emoji_list == ["😎"]
    m.reply_sticker.assert_awaited_once_with("registered-sticker")
    m.reply.assert_not_awaited()


def test_trimmed_video_notice_after_success(handlers):
    m = message(kind="video")
    handlers["prepare_media"] = lambda data, kind: media.PreparedMedia(kind, b"prepared", trimmed=True)
    meta = SimpleNamespace(extract_text=lambda: (m, ""))
    asyncio.run(handlers["process_sticker_chat"](m, meta, None))
    m.reply_sticker.assert_awaited_once_with("registered-sticker")
    m.reply.assert_awaited_once_with(handlers["trimmed_sticker_notice"])


def test_executor_timeout_never_uploads(handlers):
    m = message(kind="video")
    handlers["cpu_executor"].run = AsyncMock(return_value=(None, True))
    asyncio.run(handlers["process_sticker_chat"](m, None, None))
    m.bot.upload_sticker_file.assert_not_awaited()
    m.bot.add_sticker_to_set.assert_not_awaited()
    m.bot.create_new_sticker_set.assert_not_awaited()
    assert "слишком много времени" in m.reply.call_args.args[0]


@pytest.mark.parametrize("trimmed", [False, True])
def test_new_pack_preserves_prepared_video(handlers, trimmed):
    m = message(kind="video")
    m.bot.get_sticker_set.side_effect = [
        error("STICKERSET_INVALID"),
        error("STICKERSET_INVALID"),
        registered_pack("video"),
    ]
    handlers["prepare_media"] = lambda data, kind: media.PreparedMedia(kind, b"prepared", trimmed=trimmed)
    state = SimpleNamespace(set_data=AsyncMock(), set_state=AsyncMock(), clear=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (m, ""))
    asyncio.run(handlers["process_sticker_chat"](m, meta, state))
    data = state.set_data.call_args.args[0]
    assert data["mixed_sticker"]["format"] == "video"
    assert data["sticker_upload"]["file_unique_id"] == "unique-uploaded"
    assert data["sticker_trimmed"] is trimmed
    m.text = "Наш пак"
    m.caption = None
    m.bot.upload_sticker_file.reset_mock()
    asyncio.run(handlers["finish_chat_set"](m, state, data))
    call = m.bot.create_new_sticker_set.call_args
    assert call.kwargs["stickers"][0].sticker == "uploaded"
    state.clear.assert_awaited_once()
    m.reply_sticker.assert_awaited_once_with("registered-sticker")
    assert (handlers["trimmed_sticker_notice"] in m.reply.call_args.args[0]) is trimmed


def test_creation_error_keeps_pending_state(handlers):
    m = message()
    m.text = "Пак"
    m.caption = None
    m.bot.get_sticker_set.side_effect = error("STICKERSET_INVALID")
    m.bot.create_new_sticker_set.side_effect = error("failure")
    state = SimpleNamespace(clear=AsyncMock())
    data = dict(
        sticker_chat_id=-100,
        sticker_user_id=1,
        sticker_set_name="pack",
        mixed_sticker={"sticker": "uploaded", "format": "static", "emoji_list": ["✨"]},
    )
    with pytest.raises(TelegramBadRequest):
        asyncio.run(handlers["finish_chat_set"](m, state, data))
    state.clear.assert_not_awaited()


def test_delete_other_chat_pack(handlers):
    m = message(kind="video")
    m.reply_to_message.sticker.set_name = "with_love_for_999_by_msu_hub_bot"
    asyncio.run(handlers["process_sticker_delete"](m))
    m.bot.delete_sticker_from_set.assert_not_awaited()


def test_revoked_admin_cannot_finish_pack(handlers):
    m = message(admin=False)
    m.text = "Пак"
    m.caption = None
    state = SimpleNamespace(clear=AsyncMock())
    data = dict(
        sticker_chat_id=-100,
        sticker_user_id=1,
        sticker_set_name="pack",
        mixed_sticker={"sticker": "file", "format": "video", "emoji_list": ["✨"]},
    )
    asyncio.run(handlers["finish_chat_set"](m, state, data))
    m.bot.upload_sticker_file.assert_not_awaited()
    m.bot.add_sticker_to_set.assert_not_awaited()
    m.bot.create_new_sticker_set.assert_not_awaited()


def test_creation_reuses_concurrently_created_pack(handlers):
    m = message(kind="video")
    m.text = "Пак"
    m.caption = None
    state = SimpleNamespace(clear=AsyncMock())
    data = dict(
        sticker_chat_id=-100,
        sticker_user_id=1,
        sticker_set_name="pack",
        mixed_sticker={"sticker": "file", "format": "video", "emoji_list": ["✨"]},
    )
    asyncio.run(handlers["finish_chat_set"](m, state, data))
    m.bot.add_sticker_to_set.assert_awaited_once()
    state.clear.assert_awaited_once()
    m.reply_sticker.assert_awaited_once_with("registered-sticker")


@pytest.mark.parametrize("failure", ["lookup", "send"])
def test_saved_sticker_preview_failure_never_repeats_the_addition(handlers, failure):
    m = message(kind="video")
    handlers["prepare_media"] = lambda data, kind: media.PreparedMedia(kind, b"prepared", trimmed=True)
    meta = SimpleNamespace(extract_text=lambda: (m, ""))
    if failure == "lookup":
        m.bot.get_sticker_set.side_effect = [registered_pack("video"), error("unavailable", network=True)]
    else:
        m.reply_sticker.side_effect = error("preview rejected")
    asyncio.run(handlers["process_sticker_chat"](m, meta, None))
    m.bot.upload_sticker_file.assert_awaited_once()
    m.bot.add_sticker_to_set.assert_awaited_once()
    m.bot.create_new_sticker_set.assert_not_awaited()
    assert "Стикер добавлен" in m.reply.call_args.args[0]
    assert "https://t.me/addstickers/" in m.reply.call_args.args[0]
    assert m.reply.call_args.args[0].count(handlers["trimmed_sticker_notice"]) == 1
    if failure == "lookup":
        m.reply_sticker.assert_not_awaited()
    else:
        m.reply_sticker.assert_awaited_once_with("registered-sticker")


def test_successful_creation_clears_pending_state_even_when_preview_fails(handlers):
    m = message(kind="video")
    m.text = "Наш пак"
    m.caption = None
    m.bot.get_sticker_set.side_effect = [
        error("STICKERSET_INVALID"),
        error("preview unavailable", network=True),
    ]
    state = SimpleNamespace(clear=AsyncMock())
    data = dict(
        sticker_chat_id=-100,
        sticker_user_id=1,
        sticker_set_name="pack",
        mixed_sticker={"sticker": "uploaded", "format": "video", "emoji_list": ["✨"]},
        sticker_trimmed=True,
    )
    asyncio.run(handlers["finish_chat_set"](m, state, data))
    state.clear.assert_awaited_once()
    m.bot.create_new_sticker_set.assert_awaited_once()
    m.bot.add_sticker_to_set.assert_not_awaited()
    m.reply_sticker.assert_not_awaited()
    assert "Стикер добавлен" in m.reply.call_args.args[0]
    assert handlers["trimmed_sticker_notice"] in m.reply.call_args.args[0]


def test_oversized_video_is_encoded_once(monkeypatch):
    monkeypatch.setattr(media, "_probe", lambda path: ({}, {"codec_name": "h264"}, 6))
    calls = []

    def encode(command):
        calls.append(command)
        Path(command[-1]).write_bytes(b"x" * (media.MAX_VIDEO_BYTES + 1))

    monkeypatch.setattr(media, "_run", encode)
    with pytest.raises(media.StickerMediaError, match="одной попытки"):
        media.prepare_video(b"video")
    assert len(calls) == 1


def test_encoder_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(media, "_probe", lambda path: ({}, {"codec_name": "h264"}, 6))
    calls = []

    def fail(command):
        calls.append(command)
        raise media.StickerMediaError("Ошибка кодирования")

    monkeypatch.setattr(media, "_run", fail)
    with pytest.raises(media.StickerMediaError, match="Ошибка кодирования"):
        media.prepare_video(b"video")
    assert len(calls) == 1


def test_oversized_static_is_encoded_once(monkeypatch):
    source = io.BytesIO()
    Image.new("RGB", (32, 32)).save(source, format="PNG")
    calls = []

    def save(image, output, **kwargs):
        calls.append(kwargs)
        output.write(b"x" * (512 * 1024 + 1))

    monkeypatch.setattr(Image.Image, "save", save)
    with pytest.raises(media.StickerMediaError, match="одной попытки"):
        media.prepare_static(source.getvalue())
    assert len(calls) == 1


@pytest.mark.parametrize("mime", ["application/x-tgsticker", "application/json", "application/octet-stream"])
def test_unsupported_document_never_uses_avatar(handlers, mime):
    m = message()
    m.reply_to_message.sticker = None
    m.reply_to_message.document = SimpleNamespace(mime_type=mime)
    asyncio.run(handlers["process_sticker_chat"](m, None, None))
    handlers["extract_image"].assert_not_awaited()
    m.bot.upload_sticker_file.assert_not_awaited()
    m.bot.add_sticker_to_set.assert_not_awaited()
    m.bot.create_new_sticker_set.assert_not_awaited()
    assert m.reply.call_count == 1


def test_decompression_bomb_becomes_media_error(monkeypatch):
    source = io.BytesIO()
    Image.new("RGB", (20, 20)).save(source, format="PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(media.StickerMediaError, match="картинку"):
        media.prepare_media(source.getvalue(), "static")


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
def test_input_byte_limit_applies_before_decoding(kind):
    with pytest.raises(media.StickerMediaError, match="20 МБ"):
        media.prepare_media(b"x" * (media.MAX_INPUT_BYTES + 1), kind)


def test_oversized_metadata_rejected_before_download(handlers):
    m = message(kind="video")
    m.reply_to_message.sticker.file_size = media.MAX_INPUT_BYTES + 1
    handlers["download"] = AsyncMock()
    asyncio.run(handlers["process_sticker_chat"](m, None, None))
    handlers["download"].assert_not_awaited()
    handlers["cpu_executor"].run.assert_not_awaited()
    m.bot.upload_sticker_file.assert_not_awaited()
    m.bot.add_sticker_to_set.assert_not_awaited()
    m.bot.create_new_sticker_set.assert_not_awaited()


@pytest.mark.parametrize("data", [b"x" * (64 * 1024 + 1), gzip.compress(b"x" * (2 * 1024 * 1024 + 1))])
def test_tgs_compressed_and_expanded_limits(data):
    with pytest.raises(media.StickerMediaError):
        media.prepare_tgs(data)


def test_subprocess_deadline_becomes_media_error(monkeypatch):
    def time_out(command, **kwargs):
        assert kwargs == dict(check=True, capture_output=True, timeout=60)
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr(media.subprocess, "run", time_out)
    with pytest.raises(media.StickerMediaError, match="слишком много времени"):
        media._run(["ffmpeg", "synthetic-input"])


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and FFprobe required")
@pytest.mark.parametrize("sar,expected_size", [("2/1", (512, 192)), ("1/2", (340, 512))])
def test_video_preserves_display_aspect_ratio(tmp_path, sar, expected_size):
    source = tmp_path / "anamorphic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:r=30:d=0.2",
            "-vf",
            f"setsar={sar}",
            "-c:v",
            "libx264",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    output = tmp_path / "sticker.webm"
    output.write_bytes(media.prepare_video(source.read_bytes()).payload)
    _, stream, duration = media._probe(output)
    assert (stream["width"], stream["height"]) == expected_size
    assert stream["sample_aspect_ratio"] == "1:1"
    assert 0 < duration <= 3


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and FFprobe required")
@pytest.mark.parametrize("suffix,kind", [("mp4", "video"), ("gif", "video"), ("gif", "static")])
def test_real_long_clip_uses_only_first_seven_seconds(tmp_path, suffix, kind):
    source = tmp_path / f"timeline.{suffix}"
    # Red: 0-3s; green: 3-7s; blue: 7-9s. Blue must never reach the sticker.
    frames = b"".join(bytes(color) * (64 * 64 * count) for color, count in [((255, 0, 0), 30), ((0, 255, 0), 40), ((0, 0, 255), 20)])
    codec = ["-c:v", "libx264", "-pix_fmt", "yuv420p"] if suffix == "mp4" else ["-loop", "0"]
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s:v",
            "64x64",
            "-r",
            "10",
            "-i",
            "pipe:0",
            *codec,
            "-y",
            str(source),
        ],
        input=frames,
        check=True,
        capture_output=True,
        timeout=30,
    )
    prepared = media.prepare_media(source.read_bytes(), kind)
    assert prepared.kind == "video" and prepared.trimmed
    output = tmp_path / "sticker.webm"
    output.write_bytes(prepared.payload)
    _, stream, duration = media._probe(output)
    assert 2.8 < duration <= 3
    assert max(stream["width"], stream["height"]) == 512
    assert len(prepared.payload) <= media.MAX_VIDEO_BYTES
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(output), "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    colors = [tuple(decoded[index : index + 3]) for index in range(0, len(decoded), 3)]
    assert any(red > max(green, blue) + 40 for red, green, blue in colors)
    assert any(green > max(red, blue) + 40 for red, green, blue in colors)
    assert not any(blue > max(red, green) + 40 for red, green, blue in colors)

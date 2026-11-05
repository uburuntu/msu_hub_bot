"""Personal and chat packs share artwork selection, emoji choice and preview delivery."""

import io
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetStickerSet
from aiogram.types import PhotoSize, Sticker, StickerSet
from PIL import Image

from msu_hub_bot.commands import sticker, sticker_emoji
from msu_hub_bot.commands.sticker_emoji import StickerEmojiPicker, preview_source, select_emojis
from msu_hub_bot.commands.sticker_input import parse_metadata
from msu_hub_bot.media.sticker_media import PreparedMedia, emoji_frames, emoji_preview
from msu_hub_bot.providers.jev import JevError, JevErrorReason
from msu_hub_bot.telegram import sticker_sets
from msu_hub_bot.telegram.sticker_sets import StickerSetClient, UploadedSticker
from msu_hub_bot.telemetry import Telemetry
from telegram_helpers import make_bot, make_message

THUMBNAIL = {"file_id": "thumb", "file_unique_id": "thumb", "width": 320, "height": 180}
ANIMATION = {"file_id": "gif", "file_unique_id": "gif", "width": 480, "height": 270, "duration": 4, "thumbnail": THUMBNAIL}
GIF_DOCUMENT = {"file_id": "gif", "file_unique_id": "gif", "mime_type": "video/mp4", "file_name": "gif.mp4"}


@pytest.fixture(autouse=True)
def immediate_read_retries(monkeypatch):
    monkeypatch.setattr(sticker_sets, "LOOKUP_DELAYS", (0, 0, 0))


def regular(unique_id, kind="video", *, emoji="🐸", file_id=None):
    return Sticker(
        file_id=file_id or "file-" + unique_id,
        file_unique_id=unique_id,
        type="regular",
        width=512,
        height=512,
        is_animated=kind == "animated",
        is_video=kind == "video",
        emoji=emoji,
        set_name="frogs",
        file_size=1000,
    )


def missing_pack():
    return TelegramBadRequest(method=GetStickerSet(name="pack"), message="Bad Request: STICKERSET_INVALID")


def executor():
    async def run_prepared(prepare, function, **kwargs):
        return function(*(await prepare())), False

    async def run(function, *args, **kwargs):
        return function(*args), False

    return SimpleNamespace(run_prepared=AsyncMock(side_effect=run_prepared), run=AsyncMock(side_effect=run))


def meta(text=""):
    return SimpleNamespace(text=text, extract_text=lambda: (None, ""))


def state_for(bot, message):
    return FSMContext(MemoryStorage(), StorageKey(bot_id=bot.id, chat_id=message.chat.id, user_id=message.from_user.id))


async def test_s_converts_a_gif_instead_of_saving_the_senders_avatar(monkeypatch):
    bot = make_bot()
    reply = make_message(bot, message_id=1, animation=ANIMATION, document=GIF_DOCUMENT)
    message = make_message(bot, message_id=2, text="/s", reply_to_message=reply.model_dump())
    prepared = Mock(return_value=PreparedMedia("video", b"webm"))
    monkeypatch.setattr(sticker, "prepare_media", prepared)
    monkeypatch.setattr(sticker, "download", AsyncMock(return_value=io.BytesIO(b"mp4")))
    monkeypatch.setattr(bot, "get_user_profile_photos", AsyncMock())
    monkeypatch.setattr(bot, "upload_sticker_file", AsyncMock(return_value=SimpleNamespace(file_id="uploaded", file_unique_id="new")))
    monkeypatch.setattr(bot, "get_sticker_set", AsyncMock(side_effect=missing_pack()))
    state = state_for(bot, message)

    await sticker.process_sticker(message, meta(), state, bot, executor())

    prepared.assert_called_once_with(b"mp4", "video")
    bot.get_user_profile_photos.assert_not_awaited()
    assert bot.get_sticker_set.await_args.args[0] == "with_love_for_42_by_msu_hub_bot"
    assert await state.get_state() == sticker.StickerStates.sticker_set_name.state
    draft = sticker.ChatStickerDraft.model_validate(await state.get_data())
    assert draft.sticker_personal and draft.mixed_sticker.format == "video"


@pytest.mark.parametrize("kind", ["video", "animated", "static"])
async def test_s_copies_existing_stickers_by_file_id_with_their_own_emoji(monkeypatch, kind):
    bot = make_bot()
    source = regular("source", kind)
    message = make_message(bot, message_id=2, text="/sa", reply_to_message=make_message(bot, sticker=source.model_dump()).model_dump())
    monkeypatch.setattr(sticker, "download", AsyncMock())
    # Telegram re-encodes the cross-pack copy with a new identity.
    copy = regular("copy", kind)
    packs = iter([StickerSet(name="pack", title="Pack", sticker_type="regular", stickers=[regular("older", kind)])])
    after = StickerSet(name="pack", title="Pack", sticker_type="regular", stickers=[regular("older", kind), copy])
    monkeypatch.setattr(bot, "get_sticker_set", AsyncMock(side_effect=lambda name: next(packs, after)))
    monkeypatch.setattr(bot, "add_sticker_to_set", AsyncMock(return_value=True))
    monkeypatch.setattr(bot, "download", AsyncMock(return_value=None))
    reply_sticker = AsyncMock()
    monkeypatch.setattr(type(message), "reply_sticker", reply_sticker)

    await sticker.process_sticker(message, meta(), state_for(bot, message), bot, executor())

    sticker.download.assert_not_awaited()
    added = bot.add_sticker_to_set.await_args.kwargs["sticker"]
    assert added.sticker == "file-source" and added.format == kind and added.emoji_list == ["🐸"]
    reply_sticker.assert_awaited_once_with("file-copy")


async def test_explicit_emoji_and_keywords_override_the_source_sticker_emoji(monkeypatch):
    picker = SimpleNamespace(pick=AsyncMock(return_value=("🐱",)))
    emojis = await sticker.choose_emojis(parse_metadata("😀 | кот"), regular("source"), None, executor(), picker)
    assert emojis == ("😀",)
    picker.pick.assert_not_awaited()


async def test_emoji_chain_prefers_sticker_emoji_then_jev_then_replied_text():
    photo = PhotoSize(file_id="photo", file_unique_id="photo", width=640, height=480)
    picker = SimpleNamespace(pick=AsyncMock(return_value=("🐱", "😊")))
    assert await sticker.choose_emojis(parse_metadata("", "😎"), regular("source"), None, executor(), picker) == ("🐸",)
    picker.pick.assert_not_awaited()
    assert await sticker.choose_emojis(parse_metadata("", "😎"), photo, None, executor(), picker) == ("🐱", "😊")
    picker.pick.return_value = ()
    assert await sticker.choose_emojis(parse_metadata("", "😎"), photo, None, executor(), picker) == ("😎",)
    assert await sticker.choose_emojis(parse_metadata(""), photo, None, executor(), None) == ("✨",)


@pytest.mark.parametrize(
    "probabilities,expected",
    [
        ({"cat": 0.5, "smiling": 0.25, "love": 0.2, "dog": 0.05}, ("🐱", "😊", "😍")),
        ({"cat": 0.3, "smiling": 0.19, "love": 0.19}, ("🐱",)),
        ({"cat": 0.29, "smiling": 0.29, "love": 0.29}, ()),
        ({"cat": 0.25, "dog": 0.25, "fox": 0.25, "frog": 0.25}, ()),
        ({}, ()),
    ],
)
def test_jev_tags_need_a_confident_top_pick_and_clear_extras(probabilities, expected):
    assert select_emojis(probabilities) == expected


def test_preview_uses_telegram_stills_only():
    photo = PhotoSize(file_id="photo", file_unique_id="photo", width=640, height=480)
    assert preview_source(photo) is photo
    assert preview_source(make_message(animation=ANIMATION).animation).file_id == "thumb"
    assert preview_source(SimpleNamespace(thumbnail=None)) is None


def picker(client):
    return StickerEmojiPicker(client, telemetry=Telemetry())


def png(size=(100, 50), mode="RGBA"):
    data = io.BytesIO()
    Image.new(mode, size, (255, 0, 0, 0) if mode == "RGBA" else "red").save(data, format="PNG")
    return data.getvalue()


async def test_picker_sends_a_jpeg_of_the_thumbnail_and_maps_tags(monkeypatch):
    client = SimpleNamespace(sticker_emojis=AsyncMock(return_value={"cat": 0.8, "dog": 0.2}))
    download = AsyncMock(return_value=io.BytesIO(png()))
    monkeypatch.setattr(sticker_emoji, "download", download)
    animation = make_message(animation=ANIMATION).animation

    assert await picker(client).pick(animation, None, executor()) == ("🐱", "🐶")
    assert download.await_args.args[0].file_id == "thumb"
    (image,) = client.sticker_emojis.await_args.args[0]
    assert image.startswith(b"\xff\xd8\xff")


async def test_picker_sends_three_clip_frames_from_the_shared_download(monkeypatch):
    client = SimpleNamespace(sticker_emojis=AsyncMock(return_value={"muscle": 0.71, "handshake": 0.28}))
    monkeypatch.setattr(sticker_emoji, "download", AsyncMock())
    monkeypatch.setattr(sticker_emoji, "emoji_frames", Mock(return_value=(b"\xff\xd8\xff1", b"\xff\xd8\xff2", b"\xff\xd8\xff3")))
    clip = AsyncMock(return_value=b"mp4")
    animation = make_message(animation=ANIMATION).animation

    assert await picker(client).pick(animation, None, executor(), clip) == ("💪", "🤝")
    sticker_emoji.emoji_frames.assert_called_once_with(b"mp4")
    sticker_emoji.download.assert_not_awaited()
    assert len(client.sticker_emojis.await_args.args[0]) == 3


@pytest.mark.parametrize(
    "failure",
    [JevError(JevErrorReason.TIMEOUT), JevError(JevErrorReason.UNAVAILABLE), TimeoutError()],
)
async def test_picker_failures_fall_back_silently(monkeypatch, failure):
    client = SimpleNamespace(sticker_emojis=AsyncMock(side_effect=failure))
    monkeypatch.setattr(sticker_emoji, "download", AsyncMock(return_value=io.BytesIO(png())))
    photo = PhotoSize(file_id="photo", file_unique_id="photo", width=640, height=480)
    assert await picker(client).pick(photo, None, executor()) == ()


async def test_picker_skips_custom_emoji_and_media_without_stills(monkeypatch):
    client = SimpleNamespace(sticker_emojis=AsyncMock())
    monkeypatch.setattr(sticker_emoji, "download", AsyncMock())
    custom = Sticker(file_id="e", file_unique_id="e", type="custom_emoji", width=100, height=100, is_animated=False, is_video=False)
    assert await picker(client).pick(custom, None, executor()) == ()
    assert await picker(client).pick(SimpleNamespace(thumbnail=None), None, executor()) == ()
    client.sticker_emojis.assert_not_awaited()
    sticker_emoji.download.assert_not_awaited()


def test_emoji_preview_flattens_transparency_into_a_512px_jpeg():
    preview = Image.open(io.BytesIO(emoji_preview(png())))
    assert preview.format == "JPEG" and preview.size == (512, 256)
    assert preview.getpixel((10, 10)) == pytest.approx((255, 255, 255), abs=3)


async def test_personal_title_needs_no_chat_admin_rights(monkeypatch):
    bot = make_bot()
    message = make_message(bot, message_id=3, text="Мой пак", chat={"id": 42, "type": "private"})
    state = state_for(bot, message)
    data = sticker.ChatStickerDraft(
        mixed_sticker={"sticker": "uploaded", "format": "static", "emoji_list": ["✨"]},
        sticker_upload={"file_unique_id": "new"},
        sticker_set_name="with_love_for_42_by_msu_hub_bot",
        sticker_chat_id=42,
        sticker_user_id=42,
        sticker_origin_message_id=2,
        sticker_personal=True,
    ).model_dump(mode="json")
    await state.set_state(sticker.StickerStates.sticker_set_name)
    await state.set_data(data)
    monkeypatch.setattr(sticker, "can_edit_chat_stickers", AsyncMock(return_value=False))
    monkeypatch.setattr(bot, "get_sticker_set", AsyncMock(side_effect=missing_pack()))
    monkeypatch.setattr(bot, "create_new_sticker_set", AsyncMock(return_value=True))
    monkeypatch.setattr(StickerSetClient, "resolve", AsyncMock(return_value="registered"))
    from msu_hub_bot.telegram.state import ReleasableEventIsolation, UpdateStateContext

    await sticker.Stickers.sticker_set_name(message, state, bot, UpdateStateContext(eligible=True), ReleasableEventIsolation())

    sticker.can_edit_chat_stickers.assert_not_awaited()
    assert bot.create_new_sticker_set.await_args.kwargs["title"] == "Мой пак"
    assert await state.get_state() is None


async def test_legacy_personal_title_prompt_is_cleared_with_a_hint(monkeypatch):
    bot = make_bot()
    message = make_message(bot, text="Мой пак")
    state = state_for(bot, message)
    await state.set_state(sticker.StickerStates.sticker_set_name)
    await state.set_data({"sticker_set_name": "pack", "emojis": "✨", "png": "image", "tgs": ""})
    reply = AsyncMock()
    monkeypatch.setattr(type(message), "reply", reply)
    from msu_hub_bot.telegram.state import ReleasableEventIsolation, UpdateStateContext

    await sticker.Stickers.sticker_set_name(message, state, bot, UpdateStateContext(eligible=True), ReleasableEventIsolation())

    assert await state.get_state() is None and "/s" in reply.await_args.args[0]


async def test_sd_without_a_sticker_explains_itself(monkeypatch):
    bot = make_bot()
    message = make_message(bot, text="/sd")
    reply = AsyncMock()
    monkeypatch.setattr(type(message), "reply", reply)
    await sticker.process_sticker_delete(message, bot)
    assert "/sd" in reply.await_args.args[0]


def reencoded(unique_id):
    return SimpleNamespace(
        file_id="registered-" + unique_id, file_unique_id=unique_id, file_size=9, type="regular", is_animated=False, is_video=True
    )


async def test_one_new_entry_after_our_addition_identifies_a_reencoded_copy():
    before = SimpleNamespace(stickers=[reencoded("older")])
    after = SimpleNamespace(
        stickers=[reencoded("older"), reencoded("copy"), SimpleNamespace(**{**vars(reencoded("static")), "is_video": False})]
    )
    bot = SimpleNamespace(
        get_sticker_set=AsyncMock(side_effect=[before, after]),
        add_sticker_to_set=AsyncMock(return_value=True),
        download=AsyncMock(return_value=None),
        get_file=AsyncMock(),
    )
    client = StickerSetClient(bot)
    copied = UploadedSticker("source-file", "video", ("🐸",), "source")
    assert await client.save("pack", 1, copied)
    assert await client.resolve("pack", copied) == "registered-copy"


async def test_ambiguous_concurrent_additions_fall_back_to_the_pack_link():
    before = SimpleNamespace(stickers=[])
    after = SimpleNamespace(stickers=[reencoded("copy"), reencoded("another-admin")])
    bot = SimpleNamespace(
        get_sticker_set=AsyncMock(side_effect=[before, after, after, after]),
        add_sticker_to_set=AsyncMock(return_value=True),
        download=AsyncMock(return_value=None),
        get_file=AsyncMock(),
    )
    client = StickerSetClient(bot)
    copied = UploadedSticker("source-file", "video", ("🐸",), "source")
    assert await client.save("pack", 1, copied)
    assert await client.resolve("pack", copied) is None


async def test_without_our_own_addition_new_entries_are_never_guessed():
    bot = SimpleNamespace(
        get_sticker_set=AsyncMock(return_value=SimpleNamespace(stickers=[reencoded("someone-else")])),
        download=AsyncMock(return_value=None),
        get_file=AsyncMock(),
    )
    assert await StickerSetClient(bot).resolve("pack", UploadedSticker("source-file", "video", ("🐸",), "source")) is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")
@pytest.mark.parametrize("seconds", [0.2, 3, 12])
def test_emoji_frames_sample_start_middle_and_late_stills(tmp_path, seconds):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x180:rate=25:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        check=True,
    )
    frames = emoji_frames(clip.read_bytes())
    assert 1 <= len(frames) <= 3 and (seconds < 1 or len(frames) == 3)
    assert all(Image.open(io.BytesIO(frame)).format == "JPEG" for frame in frames)
    assert max(Image.open(io.BytesIO(frames[0])).size) == 512

"""Artwork selection and metadata contracts without live sticker mutations."""

import asyncio
import gzip
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Sticker, MessageEntity

from telegram_helpers import make_message
import test_sc_animation
from test_sc_animation import message
from test_sticker_sets import bot_with_pack, error, pack, registered

from msu_hub_bot.commands.sticker_input import custom_emoji_source, parse_metadata, reusable_sticker
from msu_hub_bot.media.sticker_media import PreparedMedia, StickerMediaError, prepare_custom_emoji
from msu_hub_bot.telegram.sticker_sets import StickerSetClient, UploadedSticker
from msu_hub_bot.telegram import sticker_sets


EMOJIS = "😀😃😄😁😆😅😂🤣😊😇🙂🙃😉😌😍🥰😘😗😙😚😋"


@pytest.fixture
def handlers():
    return test_sc_animation.handlers.__wrapped__()


def artwork(kind="static", **changes):
    return Sticker(
        **{
            "file_id": "source-file",
            "file_unique_id": "unique-uploaded",
            "type": "regular",
            "width": 512,
            "height": 512,
            "file_size": 100,
            "is_animated": kind == "animated",
            "is_video": kind == "video",
            **changes,
        }
    )


def test_metadata_preserves_default_and_reply_emoji_behavior():
    assert parse_metadata("").emojis == ("✨",)
    assert parse_metadata("", "А вот и 😎😎").emojis == ("😎",)
    assert not parse_metadata("", "😎").emojis_explicit


def test_metadata_supports_twenty_deduplicated_emoji_and_search_words():
    metadata = parse_metadata(EMOJIS[:20] + EMOJIS[0] + " | Кот, мем, кот, доброе   утро")
    assert metadata.emojis == tuple(EMOJIS[:20])
    assert metadata.emojis_explicit
    assert metadata.keywords == ("Кот", "мем", "доброе утро")


def test_metadata_supports_zwj_emoji_without_splitting_it():
    assert parse_metadata("👨‍👩‍👧‍👦👍🏿").emojis == ("👨‍👩‍👧‍👦", "👍🏿")


@pytest.mark.parametrize("value", [EMOJIS, "😀 | " + "x" * 65, "😀 | " + ",".join(str(i) for i in range(21)), "кот | мем", "| кот\0"])
def test_invalid_metadata_is_rejected_instead_of_truncated(value):
    with pytest.raises(StickerMediaError):
        parse_metadata(value)


def test_keyword_limits_and_explicit_clear():
    assert parse_metadata("| " + "я" * 64).keywords == ("я" * 64,)
    assert len(parse_metadata("| " + ",".join(str(i) for i in range(20))).keywords) == 20
    assert parse_metadata("😀 | , ").keywords == ()
    assert parse_metadata("😀").keywords is None
    assert parse_metadata("| 😎", "| 😎").emojis == ("✨",)


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
def test_existing_regular_artwork_is_reused_without_worker_download_or_upload(handlers, kind):
    m = message(kind=kind)
    m.reply_to_message.sticker = artwork(kind)
    handlers["download"] = AsyncMock()
    asyncio.run(handlers["process_sticker_chat"](m, None, None))
    handlers["download"].assert_not_awaited()
    handlers["cpu_executor"].run_prepared.assert_not_awaited()
    m.bot.upload_sticker_file.assert_not_awaited()
    m.bot.add_sticker_to_set.assert_not_awaited()
    m.bot.create_new_sticker_set.assert_not_awaited()
    m.reply_sticker.assert_awaited_once_with("registered-sticker")
    m.bot.set_sticker_emoji_list.assert_not_awaited()
    m.bot.set_sticker_keywords.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [{"type": "custom_emoji"}, {"width": 100, "height": 100}, {"height": 0}, {"width": 513}, {"file_size": 512 * 1024 + 1}],
)
def test_invalid_or_custom_artwork_is_not_directly_reused(changes):
    assert not reusable_sticker(artwork(**changes))


def test_metadata_survives_title_prompt_and_legacy_prompts_still_load():
    original = UploadedSticker("id", "static", ("😀",), "unique", keywords=("кот",), emojis_explicit=True)
    pending = {"mixed_sticker": original.input_sticker().model_dump(mode="json"), "sticker_upload": original.metadata()}
    assert UploadedSticker.from_pending(pending) == original
    legacy = UploadedSticker.from_pending({"mixed_sticker": {"sticker": "id", "format": "video", "emoji_list": ["✨"]}})
    assert legacy.keywords is None and not legacy.emojis_explicit


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
async def test_duplicate_metadata_changes_target_pack_sticker_not_original_source(kind):
    bot = bot_with_pack(registered("identity", kind, file_id="target-pack-id"))
    bot.set_sticker_emoji_list = AsyncMock()
    bot.set_sticker_keywords = AsyncMock()
    client = StickerSetClient(bot)
    upload = UploadedSticker("source-pack-id", kind, ("😀", "🔥"), "identity", keywords=("кот",), emojis_explicit=True)
    assert await client.save("pack", 1, upload)
    resolved = await client.resolve("pack", upload)
    assert await client.update_metadata(resolved, upload)
    bot.set_sticker_emoji_list.assert_awaited_once_with(sticker="target-pack-id", emoji_list=["😀", "🔥"])
    bot.set_sticker_keywords.assert_awaited_once_with(sticker="target-pack-id", keywords=["кот"])
    bot.add_sticker_to_set.assert_not_awaited()
    bot.create_new_sticker_set.assert_not_awaited()


async def test_explicit_metadata_is_applied_after_a_concurrent_duplicate_add():
    bot = bot_with_pack()
    bot.set_sticker_emoji_list = AsyncMock()
    bot.set_sticker_keywords = AsyncMock()
    client = StickerSetClient(bot)
    upload = UploadedSticker("new", "static", ("😀",), "identity", keywords=("кот",), emojis_explicit=True)
    bot.get_sticker_set.side_effect = [pack(), pack(registered("identity", "static", file_id="new-target"))]
    assert await client.save("pack", 1, upload)
    added = bot.add_sticker_to_set.call_args.kwargs["sticker"]
    assert added.keywords == ["кот"]
    assert await client.update_metadata(await client.resolve("pack", upload), upload)
    bot.set_sticker_emoji_list.assert_awaited_once_with(sticker="new-target", emoji_list=["😀"])
    bot.set_sticker_keywords.assert_awaited_once_with(sticker="new-target", keywords=["кот"])


async def test_duplicate_metadata_failure_does_not_repeat_confirmed_save():
    bot = bot_with_pack(registered("identity"))
    bot.set_sticker_keywords = AsyncMock(side_effect=error("synthetic rejection"))
    client = StickerSetClient(bot)
    upload = UploadedSticker("source", "video", ("✨",), "identity", keywords=())
    assert await client.save("pack", 1, upload)
    assert not await client.update_metadata(await client.resolve("pack", upload), upload)
    bot.add_sticker_to_set.assert_not_awaited()


@pytest.mark.parametrize("failure", ["metadata", "lookup"])
def test_unconfirmed_metadata_never_hides_or_repeats_a_successful_save(handlers, failure):
    m = message()
    m.reply_to_message.sticker = artwork()
    meta = SimpleNamespace(text="😀 | кот", extract_text=lambda: (m, "😀 | кот"))
    if failure == "metadata":
        m.bot.set_sticker_emoji_list.side_effect = error("synthetic failure")
    else:
        initial = m.bot.get_sticker_set.return_value
        m.bot.get_sticker_set.side_effect = [initial, error("lookup unavailable", network=True)]
    asyncio.run(handlers["process_sticker_chat"](m, meta, None))
    m.bot.add_sticker_to_set.assert_not_awaited()
    m.bot.upload_sticker_file.assert_not_awaited()
    assert "Стикер сохранён" in m.reply.call_args.args[0]
    assert "Обновление эмодзи и меток не подтверждено" in m.reply.call_args.args[0]


def test_telegram_transformed_copy_falls_back_without_preview_or_metadata_guess(handlers, monkeypatch):
    monkeypatch.setattr(sticker_sets, "LOOKUP_DELAYS", (0, 0, 0))
    m = message()
    m.reply_to_message.sticker = artwork(file_size=8)
    transformed = artwork(file_id="target-copy", file_unique_id="transformed-identity", file_size=8)
    m.bot.get_sticker_set.return_value = SimpleNamespace(title="Pack", stickers=[transformed])

    def download(file_id, *, destination):
        destination.write(b"old-data" if file_id == "source-file" else b"new-data")

    m.bot.download = AsyncMock(side_effect=download)
    meta = SimpleNamespace(text="😀 | кот", extract_text=lambda: (m, "😀 | кот"))
    asyncio.run(handlers["process_sticker_chat"](m, meta, None))
    m.bot.add_sticker_to_set.assert_awaited_once()
    m.bot.upload_sticker_file.assert_not_awaited()
    m.reply_sticker.assert_not_awaited()
    m.bot.set_sticker_emoji_list.assert_not_awaited()
    m.bot.set_sticker_keywords.assert_not_awaited()
    assert "https://t.me/addstickers/" in m.reply.call_args.args[0]
    assert "Обновление эмодзи и меток не подтверждено" in m.reply.call_args.args[0]


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
def test_custom_emoji_artwork_uses_regular_pack_conversion(handlers, kind):
    m = message(kind=kind)
    m.reply_to_message.sticker = artwork(kind, type="custom_emoji", custom_emoji_id="111", width=100, height=100)
    handlers["download"] = AsyncMock(return_value=io.BytesIO(b"custom"))
    handlers["prepare_custom_emoji"] = lambda data, kind: PreparedMedia(kind, b"converted")
    m.bot.get_sticker_set.side_effect = [pack(), m.bot.get_sticker_set.return_value]
    asyncio.run(handlers["process_sticker_chat"](m, None, None))
    assert handlers["cpu_executor"].run_prepared.call_args.args[1] is handlers["prepare_custom_emoji"]
    assert m.bot.upload_sticker_file.call_args.kwargs["sticker_format"] == kind
    assert m.bot.add_sticker_to_set.call_args.kwargs["sticker"].format == kind


def custom_entity(identity="111", offset=0):
    return MessageEntity(type="custom_emoji", offset=offset, length=2, custom_emoji_id=identity)


@pytest.mark.parametrize("caption", [False, True])
async def test_custom_emoji_resolves_repeated_entity_by_id(caption):
    resolved = artwork(type="custom_emoji", custom_emoji_id="111")
    bot = SimpleNamespace(get_custom_emoji_stickers=AsyncMock(return_value=[artwork(custom_emoji_id="other"), resolved]))
    fields = {
        "caption" if caption else "text": "😀😀",
        "caption_entities" if caption else "entities": [custom_entity(), custom_entity(offset=2)],
    }
    original = make_message(**fields)
    command = make_message(text="/sc", reply_to_message=original)
    assert await custom_emoji_source(command, bot) == resolved
    bot.get_custom_emoji_stickers.assert_awaited_once_with(custom_emoji_ids=["111"])


async def test_multiple_custom_emoji_are_rejected_before_api_call():
    bot = SimpleNamespace(get_custom_emoji_stickers=AsyncMock())
    with pytest.raises(StickerMediaError, match="несколько"):
        await custom_emoji_source(make_message(text="😀😀", entities=[custom_entity(), custom_entity("222", 2)]), bot)
    bot.get_custom_emoji_stickers.assert_not_awaited()


@pytest.mark.parametrize("result", [[], [artwork(custom_emoji_id="other")]])
async def test_missing_custom_emoji_never_uses_unrelated_returned_artwork(result):
    bot = SimpleNamespace(get_custom_emoji_stickers=AsyncMock(return_value=result))
    with pytest.raises(StickerMediaError, match="недоступен"):
        await custom_emoji_source(make_message(text="😀", entities=[custom_entity()]), bot)


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
def test_repainting_artwork_is_explicitly_rejected_before_download(handlers, kind):
    m = message(kind=kind)
    m.reply_to_message.sticker = artwork(kind, type="custom_emoji", needs_repainting=True)
    handlers["download"] = AsyncMock()
    asyncio.run(handlers["process_sticker_chat"](m, None, None))
    handlers["download"].assert_not_awaited()
    m.bot.add_sticker_to_set.assert_not_awaited()
    assert "меняет цвет" in m.reply.call_args.args[0]


def test_custom_tgs_normalizes_canvas_without_changing_layer_animation():
    layers = [{"ty": 4, "ind": 8, "ip": 0, "op": 60, "ks": {"r": {"a": 1, "k": [{"t": 0, "s": [0]}, {"t": 60, "s": [90]}]}}}]
    original = {"v": "5.5.2", "w": 100, "h": 50, "ip": 0, "op": 60, "fr": 30, "layers": layers, "assets": []}
    prepared = prepare_custom_emoji(gzip.compress(json.dumps(original).encode()), "animated")
    result = json.loads(gzip.decompress(prepared.payload))
    assert prepared.kind == "animated"
    assert result["w"] == result["h"] == 512
    assert result["assets"][0]["layers"] == layers
    assert result["layers"][0]["ks"]["s"]["k"] == [512, 512, 100]
    assert result["layers"][0]["ks"]["p"]["k"] == [0, 128, 0]
    assert (result["ip"], result["op"], result["fr"]) == (0, 60, 30)


@pytest.mark.parametrize("fields", [{"w": 0}, {"h": 8193}, {"w": True}, {"op": 200}, {"fr": 0}, {"layers": {}}])
def test_invalid_custom_tgs_is_rejected(fields):
    original = {"w": 100, "h": 100, "ip": 0, "op": 60, "fr": 30, "layers": [], **fields}
    with pytest.raises(StickerMediaError):
        prepare_custom_emoji(gzip.compress(json.dumps(original).encode()), "animated")


def test_already_full_size_custom_tgs_preserves_exact_bytes():
    original = gzip.compress(json.dumps({"w": 512, "h": 512, "ip": 0, "op": 60, "fr": 30, "layers": []}).encode())
    assert prepare_custom_emoji(original, "animated") == PreparedMedia("animated", original)

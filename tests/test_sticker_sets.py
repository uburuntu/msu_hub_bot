"""Telegram identity and failure contracts; no live sticker mutations."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetStickerSet
from aiogram.types import File

from telegram_helpers import make_bot

from hub_bot.utils import sticker_sets
from hub_bot.utils.sticker_sets import StickerSetClient, UploadedSticker


@pytest.fixture(autouse=True)
def immediate_read_retries(monkeypatch):
    monkeypatch.setattr(sticker_sets, "LOOKUP_DELAYS", (0, 0, 0))


def error(message, *, network=False):
    cls = TelegramNetworkError if network else TelegramBadRequest
    return cls(method=GetStickerSet(name="pack"), message=message)


def registered(unique_id, kind="video", *, file_id=None, size=4):
    return SimpleNamespace(
        file_id=file_id or "registered-" + unique_id,
        file_unique_id=unique_id,
        file_size=size,
        type="regular",
        is_animated=kind == "animated",
        is_video=kind == "video",
    )


def pack(*items):
    return SimpleNamespace(stickers=list(items))


def bot_with_pack(*items):
    return SimpleNamespace(
        get_sticker_set=AsyncMock(return_value=pack(*items)),
        get_file=AsyncMock(),
        download=AsyncMock(),
        add_sticker_to_set=AsyncMock(return_value=True),
        create_new_sticker_set=AsyncMock(return_value=True),
    )


def upload(unique_id="wanted", kind="video", payload=None):
    return UploadedSticker(
        "upload-document",
        kind,
        ("✨",),
        unique_id,
        hashlib.sha256(payload).hexdigest() if payload else None,
        len(payload) if payload else None,
    )


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
async def test_registered_identity_wins_over_pack_order(kind):
    bot = bot_with_pack(registered("older", kind), registered("wanted", kind), registered("another-admin", kind))
    client = StickerSetClient(bot)
    assert await client.resolve("pack", upload(kind=kind)) == "registered-wanted"
    bot.download.assert_not_awaited()
    bot.add_sticker_to_set.assert_not_awaited()


async def test_concurrent_saves_resolve_their_own_stickers():
    items = []
    both_added = asyncio.Event()
    bot = bot_with_pack()
    bot.get_sticker_set.side_effect = lambda name: pack(*reversed(items))

    async def add(*, user_id, name, sticker):
        identity = sticker.sticker
        items.append(registered(identity))
        if len(items) == 2:
            both_added.set()
        await asyncio.wait_for(both_added.wait(), 1)
        return True

    bot.add_sticker_to_set.side_effect = add

    async def save_and_resolve(identity):
        client = StickerSetClient(bot)
        sticker = UploadedSticker(identity, "video", ("✨",), identity)
        assert await client.save("pack", 1, sticker)
        return await client.resolve("pack", sticker)

    assert await asyncio.gather(save_and_resolve("first"), save_and_resolve("second")) == ["registered-first", "registered-second"]
    assert bot.add_sticker_to_set.await_count == 2


async def test_duplicate_addition_can_resolve_an_existing_sticker():
    bot = bot_with_pack(registered("wanted"), registered("newer"))
    client = StickerSetClient(bot)
    assert await client.save("pack", 1, upload())
    assert await client.resolve("pack", upload()) == "registered-wanted"
    bot.add_sticker_to_set.assert_awaited_once()


async def test_changed_identity_requires_matching_file_contents():
    bot = bot_with_pack(registered("changed-id"), registered("same-size-unrelated"))
    payloads = iter([b"nope", b"data"])
    bot.download.side_effect = lambda _, destination: destination.write(next(payloads))
    assert await StickerSetClient(bot).resolve("pack", upload(payload=b"data")) == "registered-changed-id"
    assert bot.download.await_count == 2


async def test_matching_bytes_with_another_format_are_not_used():
    bot = bot_with_pack(registered("different-format", "static"))
    assert await StickerSetClient(bot).resolve("pack", upload(payload=b"data")) is None
    bot.download.assert_not_awaited()


async def test_changed_identity_and_contents_never_guess_the_last_sticker():
    bot = bot_with_pack(*(registered(str(i)) for i in range(10)))
    bot.download.side_effect = lambda _, destination: destination.write(b"nope")
    assert await StickerSetClient(bot).resolve("pack", upload(payload=b"data")) is None
    assert bot.download.await_count == sticker_sets.MAX_CONTENT_LOOKUPS
    assert bot.get_sticker_set.await_count == len(sticker_sets.LOOKUP_DELAYS)
    bot.add_sticker_to_set.assert_not_awaited()


async def test_delayed_pack_update_retries_reads_only():
    bot = bot_with_pack()
    bot.get_sticker_set.side_effect = [pack(registered("older")), pack(registered("wanted"))]
    assert await StickerSetClient(bot).resolve("pack", upload()) == "registered-wanted"
    assert bot.get_sticker_set.await_count == 2
    bot.add_sticker_to_set.assert_not_awaited()


async def test_legacy_pending_upload_resolves_its_identity():
    bot = bot_with_pack(registered("wanted"))
    bot.get_file.return_value = SimpleNamespace(file_unique_id="wanted")
    pending = {"mixed_sticker": {"sticker": "upload-document", "format": "video", "emoji_list": ["✨"]}}
    assert await StickerSetClient(bot).resolve("pack", UploadedSticker.from_pending(pending)) == "registered-wanted"
    bot.get_file.assert_awaited_once_with("upload-document")


async def test_lookup_has_an_overall_deadline(monkeypatch):
    monkeypatch.setattr(sticker_sets, "LOOKUP_TIMEOUT", 0.02)
    cancelled = asyncio.Event()

    async def slow_lookup(name):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bot = bot_with_pack()
    bot.get_sticker_set.side_effect = slow_lookup
    assert await asyncio.wait_for(StickerSetClient(bot).resolve("pack", upload()), 0.5) is None
    assert cancelled.is_set()
    bot.add_sticker_to_set.assert_not_awaited()


async def test_caller_cancellation_propagates():
    started = asyncio.Event()

    async def slow_lookup(name):
        started.set()
        await asyncio.Event().wait()

    bot = bot_with_pack()
    bot.get_sticker_set.side_effect = slow_lookup
    task = asyncio.create_task(StickerSetClient(bot).resolve("pack", upload()))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    bot.add_sticker_to_set.assert_not_awaited()


async def test_lookup_network_failure_does_not_repeat_a_save():
    bot = bot_with_pack()
    bot.get_sticker_set.side_effect = error("synthetic failure", network=True)
    assert await StickerSetClient(bot).resolve("pack", upload()) is None
    bot.add_sticker_to_set.assert_not_awaited()


@pytest.mark.parametrize("create", [False, True])
async def test_uncertain_mutation_is_never_retried(create):
    bot = bot_with_pack()
    if create:
        bot.get_sticker_set.side_effect = error("STICKERSET_INVALID")
    mutation = bot.create_new_sticker_set if create else bot.add_sticker_to_set
    mutation.side_effect = error("synthetic lost response", network=True)
    with pytest.raises(TelegramNetworkError):
        await StickerSetClient(bot).save("pack", 1, upload(), title="Pack" if create else None)
    mutation.assert_awaited_once()


async def test_creation_race_reuses_the_now_existing_pack():
    bot = bot_with_pack()
    bot.get_sticker_set.side_effect = [error("STICKERSET_INVALID"), pack(registered("other-admin"))]
    bot.create_new_sticker_set.side_effect = error("name was taken concurrently")
    assert await StickerSetClient(bot).save("pack", 1, upload(), title="Pack")
    bot.create_new_sticker_set.assert_awaited_once()
    bot.add_sticker_to_set.assert_awaited_once()


async def test_rejected_creation_keeps_the_original_error():
    bot = bot_with_pack()
    bot.get_sticker_set.side_effect = error("STICKERSET_INVALID")
    failure = error("invalid title")
    bot.create_new_sticker_set.side_effect = failure
    with pytest.raises(TelegramBadRequest) as raised:
        await StickerSetClient(bot).save("pack", 1, upload(), title="Pack")
    assert raised.value is failure
    bot.create_new_sticker_set.assert_awaited_once()
    bot.add_sticker_to_set.assert_not_awaited()


@pytest.mark.parametrize("kind,suffix", [("static", "webp"), ("animated", "tgs"), ("video", "webm")])
async def test_upload_uses_native_method_and_preserves_identity(monkeypatch, kind, suffix):
    from aiogram.types import BufferedInputFile

    bot = make_bot()
    request = AsyncMock(return_value=File(file_id="uploaded", file_unique_id="identity"))
    monkeypatch.setattr(bot.session, "make_request", request)
    uploaded = await StickerSetClient(bot).upload(1, b"synthetic", kind, ["✨"])
    method = request.call_args.args[1]
    assert method.__api_method__ == "uploadStickerFile"
    assert method.sticker_format == kind
    assert isinstance(method.sticker, BufferedInputFile)
    assert method.sticker.filename == f"sticker.{suffix}"
    assert method.sticker.data == b"synthetic"
    assert uploaded.file_id == "uploaded" and uploaded.file_unique_id == "identity"


async def test_unrelated_bad_request_is_not_treated_as_missing_pack():
    bot = bot_with_pack()
    bot.get_sticker_set.side_effect = error("synthetic unrelated rejection")
    with pytest.raises(TelegramBadRequest):
        await StickerSetClient(bot).save("pack", 1, upload(), title="Pack")
    bot.create_new_sticker_set.assert_not_awaited()
    bot.add_sticker_to_set.assert_not_awaited()

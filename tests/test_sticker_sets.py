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

from msu_hub_bot.telegram import sticker_sets
from msu_hub_bot.telegram.sticker_sets import (
    MAX_PACK_STICKERS,
    MAX_SERIES_PACKS,
    PackSeriesFull,
    PackTitleRequired,
    StickerSetClient,
    UploadedSticker,
    series_index,
    series_pack_name,
    series_title,
)


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
    return SimpleNamespace(title="Pack", stickers=list(items))


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


async def test_known_duplicate_resolves_without_repeating_addition():
    bot = bot_with_pack(registered("wanted"), registered("newer"))
    client = StickerSetClient(bot)
    assert await client.save("pack", 1, upload())
    assert await client.resolve("pack", upload()) == "registered-wanted"
    bot.add_sticker_to_set.assert_not_awaited()


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
async def test_exact_registered_file_reference_is_not_added_again(kind):
    bot = bot_with_pack(registered("identity", kind, file_id="known-file"))
    bot.get_file.return_value = SimpleNamespace(file_unique_id="identity")
    source = UploadedSticker("known-file", kind, ("✨",))
    client = StickerSetClient(bot)
    assert await client.save("pack", 1, source)
    assert await client.resolve("pack", source) == "known-file"
    bot.add_sticker_to_set.assert_not_awaited()
    bot.create_new_sticker_set.assert_not_awaited()


async def test_duplicate_identity_in_another_format_does_not_skip_addition():
    bot = bot_with_pack(registered("wanted", "static"))
    assert await StickerSetClient(bot).save("pack", 1, upload(kind="video"))
    bot.add_sticker_to_set.assert_awaited_once()


async def test_creation_race_with_the_same_artwork_does_not_repeat_addition():
    bot = bot_with_pack()
    bot.get_sticker_set.side_effect = [error("STICKERSET_INVALID"), pack(registered("wanted"))]
    bot.create_new_sticker_set.side_effect = error("name was taken concurrently")
    assert await StickerSetClient(bot).save("pack", 1, upload(), title="Pack")
    bot.create_new_sticker_set.assert_awaited_once()
    bot.add_sticker_to_set.assert_not_awaited()


async def test_changed_identity_requires_matching_file_contents():
    bot = bot_with_pack(registered("changed-id"), registered("same-size-unrelated"))
    payloads = iter([b"nope", b"data"])
    bot.download.side_effect = lambda _, destination: destination.write(next(payloads))
    assert await StickerSetClient(bot).resolve("pack", upload(payload=b"data")) == "registered-changed-id"
    assert bot.download.await_count == 2


async def test_content_lookup_bounds_actual_bytes_and_closes_candidate_buffers():
    bot = bot_with_pack(registered("matching"), registered("oversized"))
    destinations = []

    def download(file_id, *, destination):
        destinations.append(destination)
        destination.write(b"data" if file_id == "registered-matching" else b"x" * 5)

    bot.download.side_effect = download
    assert await StickerSetClient(bot).resolve("pack", upload(payload=b"data")) == "registered-matching"
    assert len(destinations) == 2 and all(destination.closed for destination in destinations)


async def test_cross_pack_reuse_fingerprints_source_only_after_identity_changes():
    bot = bot_with_pack(registered("new-identity"), registered("unrelated"))
    payloads = {"source-sticker": b"data", "registered-new-identity": b"data", "registered-unrelated": b"nope"}
    destinations = []

    def download(file_id, *, destination):
        destinations.append(destination)
        destination.write(payloads[file_id])

    bot.download.side_effect = download
    source = UploadedSticker("source-sticker", "video", ("✨",), "old-identity")
    assert await StickerSetClient(bot).resolve("pack", source) == "registered-new-identity"
    assert [call.args[0] for call in bot.download.call_args_list] == ["source-sticker", "registered-unrelated", "registered-new-identity"]
    assert all(destination.closed for destination in destinations)
    bot.add_sticker_to_set.assert_not_awaited()


async def test_missing_identity_fingerprint_is_attempted_only_once_and_is_bounded():
    bot = bot_with_pack(registered("unrelated"))
    destinations = []

    def oversized(file_id, *, destination):
        destinations.append(destination)
        destination.write(b"x" * (sticker_sets.MAX_STICKER_BYTES + 1))

    bot.download.side_effect = oversized
    assert await StickerSetClient(bot).resolve("pack", upload("old-identity")) is None
    bot.download.assert_awaited_once()
    assert destinations[0].closed


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


FIRST = "with_love_for_42_by_msu_hub_bot"
SECOND = "with_love_for_42_2_by_msu_hub_bot"


def full_pack(title="Наш пак", prefix="full"):
    return SimpleNamespace(title=title, stickers=[registered(f"{prefix}-{i}") for i in range(MAX_PACK_STICKERS)])


def series_bot(packs):
    bot = bot_with_pack()

    async def lookup(name):
        if name in packs:
            return packs[name]
        raise error("STICKERSET_INVALID")

    bot.get_sticker_set.side_effect = lookup
    return bot


def test_series_names_number_later_packs_before_the_bot_suffix():
    assert series_pack_name(FIRST, 1) == FIRST
    assert series_pack_name(FIRST, 2) == SECOND
    assert series_pack_name("pack", 3) == "pack_3"
    assert series_index(FIRST, FIRST) == 1
    assert series_index("with_love_for_42_12_by_msu_hub_bot", FIRST) == 12
    for foreign in (
        "with_love_for_42a_by_msu_hub_bot",
        "with_love_for_42_1_by_msu_hub_bot",
        "with_love_for_42_02_by_msu_hub_bot",
        "with_love_for_421_by_msu_hub_bot",
        "with_love_for_4_2_by_msu_hub_bot",
        "with_love_for_42_2_by_other_bot",
    ):
        assert series_index(foreign, FIRST) is None


def test_series_titles_keep_the_number_within_telegram_limits():
    assert series_title("Наш пак", 2) == "Наш пак (2)"
    long = series_title("x" * 64, 10)
    assert len(long) == 64 and long.endswith(" (10)")


async def test_missing_first_pack_asks_for_a_title():
    bot = series_bot({})
    with pytest.raises(PackTitleRequired):
        await StickerSetClient(bot).save(FIRST, 1, upload())
    bot.create_new_sticker_set.assert_not_awaited()


async def test_full_pack_starts_the_next_one_with_its_title():
    bot = series_bot({FIRST: full_pack()})
    client = StickerSetClient(bot)
    saved = await client.save(FIRST, 7, upload())
    assert (saved.name, saved.index, saved.started_next_pack) == (SECOND, 2, True)
    kwargs = bot.create_new_sticker_set.await_args.kwargs
    assert (kwargs["name"], kwargs["title"], kwargs["user_id"]) == (SECOND, "Наш пак (2)", 7)
    bot.add_sticker_to_set.assert_not_awaited()


async def test_first_pack_with_room_takes_the_sticker():
    bot = series_bot({FIRST: full_pack(), SECOND: pack(registered("other"))})
    saved = await StickerSetClient(bot).save(FIRST, 1, upload())
    assert (saved.name, saved.started_next_pack) == (SECOND, False)
    assert bot.add_sticker_to_set.await_args.kwargs["name"] == SECOND
    bot.create_new_sticker_set.assert_not_awaited()


async def test_room_in_an_earlier_pack_is_used_without_reading_later_ones():
    bot = series_bot({FIRST: pack(registered("other")), SECOND: full_pack()})
    saved = await StickerSetClient(bot).save(FIRST, 1, upload())
    assert saved.name == FIRST
    assert [call.args[0] for call in bot.get_sticker_set.await_args_list] == [FIRST]


async def test_duplicate_in_a_full_pack_is_not_added_again():
    first = full_pack()
    first.stickers[5] = registered("wanted")
    bot = series_bot({FIRST: first})
    saved = await StickerSetClient(bot).save(FIRST, 1, upload())
    assert saved.name == FIRST and not saved.created
    bot.add_sticker_to_set.assert_not_awaited()
    bot.create_new_sticker_set.assert_not_awaited()


async def test_pack_filled_concurrently_moves_on_to_the_next():
    bot = series_bot({FIRST: SimpleNamespace(title="Наш пак", stickers=full_pack().stickers[1:])})
    bot.add_sticker_to_set.side_effect = error("STICKERS_TOO_MUCH")
    saved = await StickerSetClient(bot).save(FIRST, 1, upload())
    assert saved.name == SECOND and saved.started_next_pack
    bot.add_sticker_to_set.assert_awaited_once()


async def test_series_has_a_bounded_number_of_packs():
    packs = {series_pack_name(FIRST, index): full_pack(prefix=str(index)) for index in range(1, MAX_SERIES_PACKS + 1)}
    bot = series_bot(packs)
    with pytest.raises(PackSeriesFull):
        await StickerSetClient(bot).save(FIRST, 1, upload())
    assert bot.get_sticker_set.await_count == MAX_SERIES_PACKS
    bot.add_sticker_to_set.assert_not_awaited()
    bot.create_new_sticker_set.assert_not_awaited()


async def test_next_save_starts_at_the_remembered_open_pack():
    packs = {FIRST: full_pack(), SECOND: pack(registered("other"))}
    bot = series_bot(packs)
    await StickerSetClient(bot).save(FIRST, 1, upload("one"))
    bot.get_sticker_set.reset_mock()
    saved = await StickerSetClient(bot).save(FIRST, 1, upload("two"))
    assert saved.name == SECOND
    assert [call.args[0] for call in bot.get_sticker_set.await_args_list] == [SECOND]


async def test_pack_started_from_the_cache_keeps_the_first_title():
    packs = {FIRST: full_pack(), SECOND: pack(registered("other"))}
    bot = series_bot(packs)
    await StickerSetClient(bot).save(FIRST, 1, upload("one"))
    packs[SECOND] = full_pack(title="Наш пак (2)", prefix="second")
    saved = await StickerSetClient(bot).save(FIRST, 1, upload("two"))
    assert saved.started_next_pack and saved.index == 3
    assert bot.create_new_sticker_set.await_args.kwargs["title"] == "Наш пак (3)"


async def test_vanished_remembered_pack_reads_the_series_again():
    packs = {FIRST: full_pack(), SECOND: pack(registered("other"))}
    bot = series_bot(packs)
    await StickerSetClient(bot).save(FIRST, 1, upload("one"))
    del packs[SECOND]
    bot.get_sticker_set.reset_mock()
    saved = await StickerSetClient(bot).save(FIRST, 1, upload("two"))
    assert saved.name == SECOND and saved.started_next_pack
    assert [call.args[0] for call in bot.get_sticker_set.await_args_list] == [SECOND, FIRST, SECOND]


async def test_forgetting_reuses_a_place_freed_in_an_earlier_pack():
    packs = {FIRST: full_pack(), SECOND: pack(registered("other"))}
    bot = series_bot(packs)
    await StickerSetClient(bot).save(FIRST, 1, upload("one"))
    packs[FIRST] = SimpleNamespace(title="Наш пак", stickers=full_pack().stickers[1:])
    sticker_sets.forget_open_pack(FIRST)
    assert (await StickerSetClient(bot).save(FIRST, 1, upload("two"))).name == FIRST

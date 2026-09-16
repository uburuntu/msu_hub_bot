"""Sticker title transitions use real FSM storage and application event isolation."""

import asyncio
import io
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import CreateNewStickerSet, SendSticker
from aiogram.types import InputSticker, Sticker, StickerSet, Update
from PIL import Image

from common.tg.state import ReleasableEventIsolation, StateContextMiddleware
from hub_bot.commands import sticker
from hub_bot.utils.sticker_sets import StickerSetClient, UploadMetadata
from telegram_helpers import make_bot, make_message


def draft(message, origin=1):
    return sticker.ChatStickerDraft(
        mixed_sticker=InputSticker(sticker="uploaded", format="video", emoji_list=["✨"]),
        sticker_upload=UploadMetadata(file_unique_id="identity"),
        sticker_set_name="pack",
        sticker_chat_id=message.chat.id,
        sticker_user_id=message.from_user.id,
        sticker_origin_message_id=origin,
    ).model_dump(mode="json")


def state_for(bot, message):
    return FSMContext(MemoryStorage(), StorageKey(bot_id=bot.id, chat_id=message.chat.id, user_id=message.from_user.id, thread_id=9))


async def invoke(message, state, bot, isolation):
    async def handle(event, data):
        async with isolation.lock(state.key):
            return await sticker.Stickers.sticker_set_name(message, state, bot, data["state_context"], isolation)

    return await StateContextMiddleware()(handle, Update(update_id=message.message_id, message=message), {})


@pytest.fixture(autouse=True)
def admin_and_preview(monkeypatch):
    monkeypatch.setattr(sticker, "can_edit_chat_stickers", AsyncMock(return_value=True))
    monkeypatch.setattr(StickerSetClient, "resolve", AsyncMock(return_value="registered-sticker"))
    yield
    assert not sticker._pending_saves


async def test_mixed_title_duplicate_is_blocked_and_late_success_preserves_new_draft(monkeypatch):
    bot = make_bot()
    message = make_message(bot, message_id=2, text="Наш пак", is_topic_message=True, message_thread_id=9)
    state, isolation = state_for(bot, message), ReleasableEventIsolation()
    original = draft(message)
    await state.set_state(sticker.StickerStates.sticker_set_name)
    await state.set_data(original)
    started, finish = asyncio.Event(), asyncio.Event()

    async def save(*args, **kwargs):
        started.set()
        await finish.wait()
        return True

    save_mock = AsyncMock(side_effect=save)
    monkeypatch.setattr(StickerSetClient, "save", save_mock)
    task = asyncio.create_task(invoke(message, state, bot, isolation))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(invoke(message.model_copy(update={"message_id": 3}), state, bot, isolation), 1)
        assert save_mock.await_count == 1
        assert "уже сохраняется" in bot.session.methods[-1].text
        async with isolation.lock(state.key):
            await state.clear()  # /cancel remains available after work starts.
            await state.set_state(sticker.StickerStates.sticker_set_name)
            newer = draft(message, origin=4)
            await state.set_data(newer)
        finish.set()
        await asyncio.wait_for(task, 1)
        assert await state.get_state() == sticker.StickerStates.sticker_set_name.state
        assert await state.get_data() == newer
        assert any(isinstance(method, SendSticker) and method.sticker == "registered-sticker" for method in bot.session.methods)
        assert isolation.key_count == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await isolation.close()
        await state.storage.close()


async def test_mixed_title_success_clears_only_the_unchanged_draft(monkeypatch):
    bot = make_bot()
    message = make_message(bot, text="Наш пак")
    state, isolation = state_for(bot, message), ReleasableEventIsolation()
    await state.set_state(sticker.StickerStates.sticker_set_name)
    await state.set_data(draft(message))
    monkeypatch.setattr(StickerSetClient, "save", AsyncMock(return_value=True))
    await invoke(message, state, bot, isolation)
    assert await state.get_state() is None and await state.get_data() == {}
    assert isolation.key_count == 0
    await isolation.close()
    await state.storage.close()


async def test_mixed_save_failure_keeps_retry_data_and_clears_guard(monkeypatch):
    bot = make_bot()
    message = make_message(bot, text="Наш пак")
    state, isolation = state_for(bot, message), ReleasableEventIsolation()
    original = draft(message)
    await state.set_state(sticker.StickerStates.sticker_set_name)
    await state.set_data(original)
    error = TelegramBadRequest(
        method=CreateNewStickerSet(user_id=42, name="pack", title="Pack", stickers=[]), message="synthetic rejection"
    )
    save = AsyncMock(side_effect=error)
    monkeypatch.setattr(StickerSetClient, "save", save)
    with pytest.raises(TelegramBadRequest):
        await invoke(message, state, bot, isolation)
    assert await state.get_data() == original
    assert await state.get_state() == sticker.StickerStates.sticker_set_name.state
    assert not sticker._pending_saves and isolation.key_count == 0
    save.side_effect = None
    save.return_value = True
    await invoke(message, state, bot, isolation)
    assert save.await_count == 2 and await state.get_state() is None
    await isolation.close()
    await state.storage.close()


async def test_cancellation_releases_transient_mixed_save_guard(monkeypatch):
    bot = make_bot()
    message = make_message(bot, text="Наш пак")
    state, isolation = state_for(bot, message), ReleasableEventIsolation()
    original = draft(message)
    await state.set_state(sticker.StickerStates.sticker_set_name)
    await state.set_data(original)
    started = asyncio.Event()

    async def save(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(StickerSetClient, "save", save)
    task = asyncio.create_task(invoke(message, state, bot, isolation))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await state.get_data() == original
    assert not sticker._pending_saves and isolation.key_count == 0
    await isolation.close()
    await state.storage.close()


async def test_personal_title_consumption_releases_lock_before_download(monkeypatch):
    bot = make_bot()
    message = make_message(bot, text="Мой пак")
    state, isolation = state_for(bot, message), ReleasableEventIsolation()
    await state.set_state(sticker.StickerStates.sticker_set_name)
    await state.set_data(sticker.PersonalStickerDraft(sticker_set_name="pack", emojis="✨", png="image").model_dump(mode="json"))
    started, finish = asyncio.Event(), asyncio.Event()
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, format="PNG")

    async def download(*args):
        started.set()
        await finish.wait()
        return io.BytesIO(image.getvalue())

    monkeypatch.setattr(sticker, "download_by_file_id", download)
    registered = Sticker(
        file_id="registered-sticker", file_unique_id="id", type="regular", width=512, height=512, is_animated=False, is_video=False
    )
    monkeypatch.setattr(
        bot, "get_sticker_set", AsyncMock(return_value=StickerSet(name="pack", title="Pack", sticker_type="regular", stickers=[registered]))
    )
    task = asyncio.create_task(invoke(message, state, bot, isolation))
    try:
        await asyncio.wait_for(started.wait(), 1)
        async with isolation.lock(state.key):
            assert await state.get_state() is None
            await state.set_state("ProgStates:stdin")
            await state.set_data({"later": "draft"})
        finish.set()
        await asyncio.wait_for(task, 1)
        assert await state.get_state() == "ProgStates:stdin" and await state.get_data() == {"later": "draft"}
        creation = next(method for method in bot.session.methods if isinstance(method, CreateNewStickerSet))
        assert creation.stickers[0].format == "static"
        assert creation.stickers[0].sticker.filename == "sticker.png"
        assert isinstance(bot.session.methods[-1], SendSticker)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await isolation.close()
        await state.storage.close()

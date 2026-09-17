import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, InlineQuery, Message, User
from pydantic import ValidationError

from common.tg.middlewares.settings import Settings, SettingsMiddleware


def message(chat_id=100):
    return Message(message_id=1, date=1_700_000_000, chat=Chat(id=chat_id, type="private", first_name="Friend"))


def callback():
    return CallbackQuery(
        id="callback", from_user=User(id=10, is_bot=False, first_name="Friend"), chat_instance="synthetic", message=message()
    )


@pytest.fixture
def storage():
    rows = {}

    async def load(chat):
        await asyncio.sleep(0)
        row = rows.setdefault(chat.chat_id, SimpleNamespace(metadata={}))
        return deepcopy(row.metadata.get("settings", {}))

    async def patch(chat_id, changes):
        rows[chat_id].metadata.setdefault("settings", {}).update(deepcopy(changes))
        return deepcopy(rows[chat_id].metadata["settings"])

    db = SimpleNamespace(load_settings=AsyncMock(side_effect=load), patch_settings=patch)
    return db, rows, db


async def test_first_contact_initializes_before_handler_and_saves(storage):
    db, rows, _ = storage

    async def handler(event, data):
        assert data["settings"].auto_speech_recognition
        data["settings"].auto_speech_recognition = False
        return "reply"

    assert await SettingsMiddleware(db)(handler, message(), {}) == "reply"
    assert rows[100].metadata["settings"]["auto_speech_recognition"] is False


async def test_concurrent_callbacks_share_preferences_and_preserve_extras(storage):
    db, rows, _ = storage
    rows[100] = SimpleNamespace(metadata={"other": 7, "settings": {"auto_video_links": False, "future_option": {"enabled": True}}})
    middleware = SettingsMiddleware(db)
    first_seen, both_seen = asyncio.Event(), asyncio.Event()
    objects = []

    async def handler(event, data):
        objects.append(data["settings"])
        if len(objects) == 1:
            first_seen.set()
            await both_seen.wait()
        else:
            both_seen.set()
        data["settings"].auto_speech_recognition = False

    first = asyncio.create_task(middleware(handler, callback(), {}))
    await first_seen.wait()
    await middleware(handler, callback(), {})
    await first
    assert objects[0] is objects[1]
    assert not objects[0].auto_video_links
    assert rows[100].metadata["other"] == 7
    assert rows[100].metadata["settings"]["future_option"] == {"enabled": True}
    db.load_settings.assert_awaited_once()


async def test_inline_query_has_no_previous_chat_preferences(storage):
    db, _, _ = storage
    seen = []

    async def handler(event, data):
        seen.append(dict(data))

    inline = InlineQuery(id="inline", query="text", offset="", from_user=User(id=10, is_bot=False, first_name="Friend"))
    await SettingsMiddleware(db)(handler, inline, {"settings": Settings()})
    assert seen == [{}]
    db.load_settings.assert_not_awaited()


async def test_pending_write_keeps_new_mutation_dirty_until_second_save(storage):
    db, rows, query = storage
    middleware = SettingsMiddleware(db)
    preferences = await middleware.proxy(message().chat)
    preferences.auto_speech_recognition = False
    started, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def update(chat_id, changes):
        snapshot = deepcopy(changes)
        writes.append(snapshot)
        if len(writes) == 1:
            started.set()
            await release.wait()
        rows[chat_id].metadata.setdefault("settings", {}).update(snapshot)
        return deepcopy(rows[chat_id].metadata["settings"])

    query.patch_settings = update
    first = asyncio.create_task(preferences.save(db))
    await started.wait()
    preferences.auto_video_links = False
    second = asyncio.create_task(preferences.save(db))
    await asyncio.sleep(0)
    assert len(writes) == 1
    release.set()
    await asyncio.gather(first, second)
    assert len(writes) == 2
    assert writes[0] == {"auto_speech_recognition": False}
    assert writes[1] == {"auto_video_links": False}
    assert rows[100].metadata["settings"]["auto_video_links"] is False
    assert rows[100].metadata["settings"]["auto_speech_recognition"] is False
    assert not preferences._is_dirty


async def test_preference_save_patches_only_changed_keys_after_an_external_update(storage):
    db, rows, _ = storage
    rows[100] = SimpleNamespace(metadata={"other": 7, "settings": {"auto_video_links": True}})
    preferences = await SettingsMiddleware(db).proxy(message().chat)
    rows[100].metadata["settings"].update({"auto_video_links": False, "future_option": [1, 2]})
    preferences.with_nsfw = True
    await preferences.save(db)
    assert rows[100].metadata == {"other": 7, "settings": {"auto_video_links": False, "future_option": [1, 2], "with_nsfw": True}}


async def test_cache_pressure_cannot_replace_an_active_preference_object(storage):
    db, _, _ = storage
    middleware = SettingsMiddleware(db, cache_size=1)
    started, finish = asyncio.Event(), asyncio.Event()
    held = []

    async def slow(event, data):
        held.append(data["settings"])
        started.set()
        await finish.wait()

    async def quick(event, data):
        return data["settings"]

    task = asyncio.create_task(middleware(slow, message(100), {}))
    await started.wait()
    await middleware(quick, message(200), {})
    same = await middleware(quick, message(100), {})
    assert same is held[0]
    finish.set()
    await task
    assert len(middleware.proxies) <= 1


async def test_handler_failure_preserves_dirty_settings_and_original_error(storage):
    db, _, query = storage
    middleware = SettingsMiddleware(db)
    original = ValueError("handler failure")
    query.patch_settings = AsyncMock(side_effect=RuntimeError("save failure"))

    async def handler(event, data):
        data["settings"].with_nsfw = True
        raise original

    with pytest.raises(ValueError) as caught:
        await middleware(handler, message(), {})
    assert caught.value is original
    assert middleware.proxies[100]._is_dirty
    assert caught.value.__notes__ == ["Chat preferences also failed to save during cleanup"]


async def test_cancellation_still_saves_preferences(storage):
    db, rows, _ = storage

    async def handler(event, data):
        data["settings"].with_nsfw = True
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await SettingsMiddleware(db)(handler, message(), {})
    assert rows[100].metadata["settings"]["with_nsfw"] is True


async def test_shutdown_attempts_other_dirty_chats_after_one_save_fails(storage):
    db, rows, query = storage
    middleware = SettingsMiddleware(db)
    first = await middleware.proxy(message(100).chat)
    second = await middleware.proxy(message(200).chat)
    first.with_nsfw = second.with_nsfw = True
    original = RuntimeError("one chat save failed")
    update = query.patch_settings

    async def save(chat_id, changes):
        if chat_id == 100:
            raise original
        return await update(chat_id, changes)

    query.patch_settings = save
    with pytest.raises(ExceptionGroup) as caught:
        await middleware.close()
    assert caught.value.exceptions == (original,)
    assert first._is_dirty
    assert rows[200].metadata["settings"]["with_nsfw"] is True
    assert not second._is_dirty


def test_preferences_do_not_read_environment_and_validate_assignment(monkeypatch):
    monkeypatch.setenv("AUTO_VIDEO_LINKS", "false")
    preferences = Settings(future_option={"value": 1})
    assert preferences.auto_video_links is True
    with pytest.raises(ValidationError):
        preferences.with_nsfw = "private-invalid-canary"
    assert preferences.with_nsfw is False
    assert preferences.model_dump()["future_option"] == {"value": 1}
    assert not any(key.startswith("_") for key in preferences.model_dump())

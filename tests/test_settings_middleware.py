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
def storage(monkeypatch):
    rows = {}

    async def insert(_type, _field, **values):
        await asyncio.sleep(0)
        rows.setdefault(values["chat_id"], SimpleNamespace(metadata={}))

    async def get(chat_id):
        return SimpleNamespace(metadata=deepcopy(rows[chat_id].metadata))

    async def update(chat_id, **values):
        rows[chat_id].metadata = deepcopy(values["metadata"])

    query = SimpleNamespace(get=get, update=update)
    monkeypatch.setattr("common.tg.middlewares.settings.ChatDB.query", lambda db: query)
    db = SimpleNamespace(insert_skip_conflict=AsyncMock(side_effect=insert))
    return db, rows, query


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
    db.insert_skip_conflict.assert_awaited_once()


async def test_inline_query_has_no_previous_chat_preferences(storage):
    db, _, _ = storage
    seen = []

    async def handler(event, data):
        seen.append(dict(data))

    inline = InlineQuery(id="inline", query="text", offset="", from_user=User(id=10, is_bot=False, first_name="Friend"))
    await SettingsMiddleware(db)(handler, inline, {"settings": Settings()})
    assert seen == [{}]
    db.insert_skip_conflict.assert_not_awaited()


async def test_pending_write_keeps_new_mutation_dirty_until_second_save(storage):
    db, rows, query = storage
    middleware = SettingsMiddleware(db)
    preferences = await middleware.proxy(message().chat)
    preferences.auto_speech_recognition = False
    started, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def update(chat_id, **values):
        snapshot = deepcopy(values["metadata"])
        writes.append(snapshot)
        if len(writes) == 1:
            started.set()
            await release.wait()
        rows[chat_id].metadata = snapshot

    query.update = update
    first = asyncio.create_task(preferences.save(db))
    await started.wait()
    preferences.auto_video_links = False
    second = asyncio.create_task(preferences.save(db))
    await asyncio.sleep(0)
    assert len(writes) == 1
    release.set()
    await asyncio.gather(first, second)
    assert len(writes) == 2
    assert writes[0]["settings"]["auto_video_links"] is True
    assert rows[100].metadata["settings"]["auto_video_links"] is False
    assert rows[100].metadata["settings"]["auto_speech_recognition"] is False
    assert not preferences._is_dirty


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
    query.update = AsyncMock(side_effect=RuntimeError("save failure"))

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


def test_preferences_do_not_read_environment_and_validate_assignment(monkeypatch):
    monkeypatch.setenv("AUTO_VIDEO_LINKS", "false")
    preferences = Settings(future_option={"value": 1})
    assert preferences.auto_video_links is True
    with pytest.raises(ValidationError):
        preferences.with_nsfw = "private-invalid-canary"
    assert preferences.with_nsfw is False
    assert preferences.model_dump()["future_option"] == {"value": 1}
    assert not any(key.startswith("_") for key in preferences.model_dump())

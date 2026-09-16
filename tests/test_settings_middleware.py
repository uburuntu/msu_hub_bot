import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, CallbackQuery, InlineQuery

from common.tg.middlewares.settings import SettingsMiddleware


@pytest.fixture
def storage(monkeypatch):
    rows = {}

    async def insert(_type, _field, **values):
        await asyncio.sleep(0)
        rows.setdefault(values["chat_id"], SimpleNamespace(metadata={}))

    async def get(chat_id):
        return rows[chat_id]

    async def update(chat_id, **values):
        rows[chat_id].metadata = values["metadata"]

    query = SimpleNamespace(get=get, update=update)
    monkeypatch.setattr("common.tg.middlewares.settings.ChatDB.query", lambda db: query)
    db = SimpleNamespace(insert_skip_conflict=AsyncMock(side_effect=insert))
    return db, rows


@pytest.mark.asyncio
async def test_first_contact_initializes_before_settings_and_saves(storage):
    db, rows = storage
    middleware = SettingsMiddleware(db)
    message = Message(chat=Chat(id=100, type="private", first_name="Friend"))
    data = {}
    await middleware.pre_process(message, data)
    assert data["settings"].auto_speech_recognition
    data["settings"].auto_speech_recognition = False
    await middleware.post_process(message, data)
    assert rows[100].metadata["settings"]["auto_speech_recognition"] is False


@pytest.mark.asyncio
async def test_concurrent_callbacks_preserve_existing_preferences(storage):
    db, rows = storage
    rows[100] = SimpleNamespace(metadata={"other": 7, "settings": {"auto_video_links": False}})
    middleware = SettingsMiddleware(db)
    query = CallbackQuery(message=Message(chat=Chat(id=100, type="group", title="Friends")))
    first, second = {}, {}
    await asyncio.gather(middleware.pre_process(query, first), middleware.pre_process(query, second))
    assert first["settings"] is second["settings"]
    assert first["settings"].auto_video_links is False
    assert rows[100].metadata == {"other": 7, "settings": {"auto_video_links": False}}
    db.insert_skip_conflict.assert_awaited_once()
    first["settings"].auto_speech_recognition = False
    await middleware.post_process(query, first)
    assert rows[100].metadata["other"] == 7


@pytest.mark.asyncio
async def test_inline_query_does_not_reuse_previous_chat_context(storage, monkeypatch):
    db, _ = storage
    middleware = SettingsMiddleware(db)
    monkeypatch.setattr(Chat, "get_current", lambda: Chat(id=999, type="private", first_name="Previous"))
    data = {}
    await middleware.pre_process(InlineQuery(id="inline", query="text"), data)
    assert data == {}
    db.insert_skip_conflict.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_during_pending_write_is_saved_by_next_handler(storage, monkeypatch):
    db, rows = storage
    middleware = SettingsMiddleware(db)
    message = Message(chat=Chat(id=100, type="private", first_name="Friend"))
    data = {}
    await middleware.pre_process(message, data)
    data["settings"].auto_speech_recognition = False
    started, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def get(chat_id):
        return SimpleNamespace(metadata=deepcopy(rows[chat_id].metadata))

    async def update(chat_id, **values):
        snapshot = deepcopy(values["metadata"])
        writes.append(snapshot)
        if len(writes) == 1:
            started.set()
            await release.wait()
        rows[chat_id].metadata = snapshot

    monkeypatch.setattr("common.tg.middlewares.settings.ChatDB.query", lambda db: SimpleNamespace(get=get, update=update))
    tasks = [asyncio.create_task(middleware.post_process(message, data))]
    try:
        await asyncio.wait_for(started.wait(), 1)
        data["settings"].auto_video_links = False
        tasks.append(asyncio.create_task(middleware.post_process(message, data)))
        await asyncio.sleep(0)
        assert len(writes) == 1  # The second write cannot overtake the first one.
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert len(writes) == 2
    assert writes[0]["settings"]["auto_video_links"] is True
    assert rows[100].metadata["settings"]["auto_video_links"] is False
    assert rows[100].metadata["settings"]["auto_speech_recognition"] is False
    assert data["settings"]._is_dirty is False

import asyncio
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

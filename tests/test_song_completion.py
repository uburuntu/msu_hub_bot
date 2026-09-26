"""Native song recognition completes one status without losing long results."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from aiogram.types import Update
from teleforge.testing import RecordingBot
from telegram_helpers import make_message

from msu_hub_bot.commands import song
from msu_hub_bot.telegram.filters import MetaCommand
from msu_hub_bot.telegram.media_jobs import DownloadUnavailable
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)


@asynccontextmanager
async def dispatcher(executor):
    dispatcher = Dispatcher(disable_fsm=True, cpu_executor=executor)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.message.middleware(SelectiveIsolationMiddleware())
    router = Router()
    router.message.register(song.process_song, MetaCommand("song", "shazam", "music"), StateFilter(None), flags={"fsm_release": True})
    dispatcher.include_router(router)
    try:
        yield dispatcher
    finally:
        await fsm.close()


def invocation(bot):
    source = make_message(
        bot,
        message_id=9,
        is_topic_message=True,
        message_thread_id=55,
        voice={"file_id": "voice", "file_unique_id": "voice", "duration": 1},
    )
    return Update(
        update_id=1, message=make_message(bot, text="/shazam", reply_to_message=source, is_topic_message=True, message_thread_id=55)
    )


def payload(title="<Track>"):
    return json.dumps(
        {
            "status": {"code": 0},
            "metadata": {
                "music": [
                    {
                        "artists": [{"name": "<Artist>"}],
                        "title": title,
                        "album": {"name": "A & B"},
                        "genres": [{"name": "Rock"}],
                        "duration_ms": 120000,
                        "release_date": "2026-01-01",
                        "external_metadata": {"youtube": {"vid": "synthetic"}, "spotify": {"track": {"id": "synthetic"}}},
                    }
                ]
            },
        }
    )


async def test_native_song_retains_literal_formatting_and_optional_artwork_preview(monkeypatch):
    bot = RecordingBot()
    executor = SimpleNamespace(run=AsyncMock(return_value=("https://example.org/art.jpg", False)))
    recognize = AsyncMock(return_value=(payload(), False))
    monkeypatch.setattr(song, "run_downloaded", recognize)
    async with dispatcher(executor) as app:
        await app.feed_update(bot, invocation(bot))
    first, artwork = [item for item in bot.requests if isinstance(item, EditMessageText)]
    assert first.text.startswith("🎙 <Artist> — <Track>") and "A & B" in first.text
    assert first.parse_mode is None and {entity.type for entity in first.entities} >= {"bold", "italic", "code", "text_link"}
    assert first.link_preview_options.is_disabled
    assert artwork.text == first.text and not artwork.link_preview_options.is_disabled
    assert artwork.link_preview_options.url == "https://example.org/art.jpg"
    assert first.message_id == artwork.message_id
    recognize.assert_awaited_once()


async def test_long_song_metadata_delivers_one_complete_file_and_does_not_replay_for_artwork(monkeypatch):
    bot = RecordingBot()
    title = "🎵" * 3000
    executor = SimpleNamespace(run=AsyncMock(return_value=("https://example.org/art.jpg", False)))
    recognize = AsyncMock(return_value=(payload(title), False))
    monkeypatch.setattr(song, "run_downloaded", recognize)
    async with dispatcher(executor) as app:
        result = await app.feed_update(bot, invocation(bot))
    files = [item for item in bot.requests if isinstance(item, SendDocument)]
    assert len(files) == 1
    file = files[0]
    assert title in file.document.data.decode() and "https://open.spotify.com/track/synthetic" in file.document.data.decode()
    assert file.reply_parameters.message_id == 9 and file.message_thread_id == 55
    assert result.document is not None and "полная информация" in bot.requests[-1].text
    recognize.assert_awaited_once()
    executor.run.assert_not_awaited()


@pytest.mark.parametrize("outcome,copy", [(None, "Не удалось распознать"), ("timeout", "Timeout"), ("download", "Не удалось распознать")])
async def test_native_song_failures_replace_the_waiting_status(monkeypatch, outcome, copy):
    bot = RecordingBot()
    recognize = AsyncMock(return_value=(None, outcome == "timeout"))
    if outcome == "download":
        recognize.side_effect = DownloadUnavailable()
    monkeypatch.setattr(song, "run_downloaded", recognize)
    async with dispatcher(SimpleNamespace(run=AsyncMock())) as app:
        await app.feed_update(bot, invocation(bot))
    assert copy in bot.requests[-1].text
    assert not any(isinstance(item, SendDocument) for item in bot.requests)


async def test_native_song_keeps_confirmed_file_when_status_notice_fails(monkeypatch):
    bot = RecordingBot()
    recognize = AsyncMock(return_value=(payload("x" * 5000), False))
    monkeypatch.setattr(song, "run_downloaded", recognize)

    async def respond(bot, method):
        if isinstance(method, EditMessageText):
            return TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
        return bot.recording._default(bot, method)

    bot.recording.responder = respond
    async with dispatcher(SimpleNamespace(run=AsyncMock())) as app:
        result = await app.feed_update(bot, invocation(bot))
    assert result.document is not None
    assert len([item for item in bot.requests if isinstance(item, SendDocument)]) == 1
    recognize.assert_awaited_once()


async def test_native_song_uncertain_upload_is_not_retried(monkeypatch):
    bot = RecordingBot()
    recognize = AsyncMock(return_value=(payload("x" * 5000), False))
    monkeypatch.setattr(song, "run_downloaded", recognize)

    async def respond(bot, method):
        if isinstance(method, SendDocument):
            return TimeoutError()
        return bot.recording._default(bot, method)

    bot.recording.responder = respond
    async with dispatcher(SimpleNamespace(run=AsyncMock())) as app:
        await app.feed_update(bot, invocation(bot))
    assert len([item for item in bot.requests if isinstance(item, SendDocument)]) == 1
    assert "мог уже прийти" in bot.requests[-1].text
    recognize.assert_awaited_once()


async def test_native_song_cancellation_clears_waiting_and_propagates(monkeypatch):
    bot = RecordingBot()
    started = asyncio.Event()

    async def recognize(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(song, "run_downloaded", recognize)
    async with dispatcher(SimpleNamespace(run=AsyncMock())) as app:
        task = asyncio.create_task(app.feed_update(bot, invocation(bot)))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert bot.requests[-1].text == "Распознавание отменено."


@pytest.mark.parametrize("cancel", [False, True])
async def test_applied_but_unconfirmed_song_edit_is_never_overwritten(monkeypatch, cancel):
    bot = RecordingBot()
    recognize = AsyncMock(return_value=(payload(), False))
    monkeypatch.setattr(song, "run_downloaded", recognize)
    applied = asyncio.Event()
    visible = {}

    async def respond(bot, method):
        if isinstance(method, EditMessageText):
            visible[method.message_id] = method.text
            applied.set()
            if cancel:
                await asyncio.Event().wait()
            return TimeoutError()
        return bot.recording._default(bot, method)

    bot.recording.responder = respond
    async with dispatcher(SimpleNamespace(run=AsyncMock())) as app:
        task = asyncio.create_task(app.feed_update(bot, invocation(bot)))
        await asyncio.wait_for(applied.wait(), 2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
            assert isinstance(bot.requests[-1], SendMessage) and bot.requests[-1].reply_parameters.message_id == 9
    assert list(visible.values())[0].startswith("🎙 <Artist>")
    assert len([item for item in bot.requests if isinstance(item, EditMessageText)]) == 1
    recognize.assert_awaited_once()


async def test_native_song_initial_status_and_complete_file_keep_direct_message_topic(monkeypatch):
    bot = RecordingBot()
    recognize = AsyncMock(return_value=(payload("x" * 5000), False))
    monkeypatch.setattr(song, "run_downloaded", recognize)

    async def respond(bot, method):
        if isinstance(method, (SendMessage, SendDocument)) and method.direct_messages_topic_id != 77:
            return TelegramBadRequest(method=method, message="Bad Request: Channel direct messages topic must be specified")
        return bot.recording._default(bot, method)

    bot.recording.responder = respond
    source = make_message(
        bot,
        message_id=9,
        direct_messages_topic={"topic_id": 77},
        voice={"file_id": "voice", "file_unique_id": "voice", "duration": 1},
    )
    incoming = make_message(bot, text="/song", reply_to_message=source, direct_messages_topic={"topic_id": 77})
    async with dispatcher(SimpleNamespace(run=AsyncMock())) as app:
        await app.feed_update(bot, Update(update_id=1, message=incoming))
    sends = [item for item in bot.requests if isinstance(item, (SendMessage, SendDocument))]
    assert len(sends) == 2 and all(item.direct_messages_topic_id == 77 for item in sends)
    assert all(item.reply_parameters.message_id == 9 for item in sends)

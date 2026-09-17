import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import Chat, Document, Message, Update, User

from common.tg.middlewares.check_gets import CheckGets
from common.tg.middlewares.logs import LoggingMiddleware
from common.tg.middlewares.settings import Settings
from common.tg.middlewares.skip777000 import Skip777000
from common.tg.middlewares.updates import UpdatesMiddleware
from common.tg.middlewares.viewer import ViewerMiddleware
from common.tg.runtime import AdmissionMiddleware, Supervisor
from hub_bot.events import EcosystemManager, EventsMiddleware
from telegram_helpers import RecordingSession


def message(**values):
    base = dict(
        message_id=1,
        date=1_700_000_000,
        chat=Chat(id=-1001234567, type="supergroup", title="Synthetic"),
        from_user=User(id=10, is_bot=False, first_name="Synthetic"),
        text="private canary text",
    )
    base.update(values)
    return Message(**base)


async def dispatch_archive(middleware, supervisor, handler, update):
    async def dispatch(event, data):
        return await middleware(handler, event, data)

    return await AdmissionMiddleware(supervisor)(dispatch, update, {})


@pytest.mark.parametrize("result,handled", [(UNHANDLED, False), (None, True)])
async def test_archive_preserves_aliases_integer_dates_and_actual_outcome(result, handled):
    supervisor = Supervisor()
    db = SimpleNamespace(archive_update=AsyncMock())
    middleware = UpdatesMiddleware(db, supervisor)
    update = Update(update_id=1, message=message())
    handler = AsyncMock(return_value=result)
    await asyncio.create_task(dispatch_archive(middleware, supervisor, handler, update))
    await supervisor.drain(1, cancel_timeout=0.1)
    values = db.archive_update.call_args.args[0].model_dump()
    assert values["handled"] is handled
    assert values["data"]["message"]["date"] == 1_700_000_000
    assert values["data"]["message"]["from"]["id"] == 10
    assert "from_user" not in values["data"]["message"]
    assert len(db.archive_update.call_args.args[0].users) == 1
    assert len(db.archive_update.call_args.args[0].chats) == 1


async def test_archive_failure_remains_owned_and_reported():
    supervisor = Supervisor()
    db = SimpleNamespace(archive_update=AsyncMock(side_effect=RuntimeError("archive failure")))
    middleware = UpdatesMiddleware(db, supervisor)
    await asyncio.create_task(
        dispatch_archive(middleware, supervisor, AsyncMock(return_value=None), Update(update_id=1, message=message()))
    )
    result = await supervisor.drain(1, cancel_timeout=0.1)
    db.archive_update.assert_awaited_once()
    assert len(db.archive_update.call_args.args[0].users) == 1
    assert len(db.archive_update.call_args.args[0].chats) == 1
    assert result.failed_jobs == 1


async def test_archive_backpressure_stays_owned_during_shutdown():
    supervisor = Supervisor()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def insert(*args, **kwargs):
        entered.set()
        await finish.wait()

    db = SimpleNamespace(archive_update=AsyncMock(side_effect=insert))
    middleware = UpdatesMiddleware(db, supervisor, concurrency=1, pending_limit=0)
    first = asyncio.create_task(
        dispatch_archive(middleware, supervisor, AsyncMock(return_value=None), Update(update_id=1, message=message()))
    )
    await entered.wait()
    await first
    second = asyncio.create_task(
        dispatch_archive(middleware, supervisor, AsyncMock(return_value=None), Update(update_id=2, message=message()))
    )
    await asyncio.sleep(0)
    assert not second.done()
    drain = asyncio.create_task(supervisor.drain(1, cancel_timeout=0.1))
    finish.set()
    await second
    await drain
    assert db.archive_update.await_count == 2


async def test_archive_handler_failure_keeps_original_error_and_queues_history():
    supervisor = Supervisor()
    db = SimpleNamespace(archive_update=AsyncMock())
    middleware = UpdatesMiddleware(db, supervisor)
    original = ValueError("synthetic handler failure")
    with pytest.raises(ValueError) as caught:
        await asyncio.create_task(
            dispatch_archive(middleware, supervisor, AsyncMock(side_effect=original), Update(update_id=1, message=message()))
        )
    assert caught.value is original
    await supervisor.drain(1, cancel_timeout=0.1)
    assert db.archive_update.call_args.args[0].handled is False


async def test_automatic_forward_stops_following_handlers_and_viewer(monkeypatch):
    unpin = AsyncMock()
    monkeypatch.setattr(Message, "unpin", unpin)
    viewer = ViewerMiddleware(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    viewer.view = AsyncMock()
    final = AsyncMock()

    async def following(event, data):
        return await viewer(final, event, data)

    assert await Skip777000()(following, message(is_automatic_forward=True), {"settings": Settings()}) is None
    unpin.assert_awaited_once()
    final.assert_not_awaited()
    viewer.view.assert_not_awaited()


async def test_milestone_reply_does_not_consume_later_handler(monkeypatch):
    order = []

    async def reply(*args, **kwargs):
        order.append("milestone")

    async def handler(event, data):
        order.append("handler")
        return "result"

    monkeypatch.setattr(Message, "reply", reply)
    assert await CheckGets()(handler, message(message_id=11111), {}) == "result"
    assert order == ["milestone", "handler"]


async def test_viewer_runs_after_unmatched_route_and_honors_video_preference():
    viewer = ViewerMiddleware(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    viewer.view = AsyncMock()
    result = await viewer(AsyncMock(return_value=UNHANDLED), message(), {"settings": Settings(auto_video_links=False)})
    assert result is UNHANDLED
    assert viewer.view.call_args.args[1].auto_video_links is False


async def test_document_without_declared_size_is_bounded_and_stream_closed(monkeypatch):
    viewer = ViewerMiddleware(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    source = io.BytesIO(b"document")
    convert = AsyncMock(return_value=("https://example.test/file.pdf", "https://example.test/thumb.jpg", "file.pdf"))
    reply = AsyncMock()
    monkeypatch.setattr("common.tg.middlewares.viewer.download", AsyncMock(return_value=source))
    monkeypatch.setattr("common.tg.middlewares.viewer.bot_for", lambda value: SimpleNamespace())
    monkeypatch.setattr("common.tg.middlewares.viewer.convert_to_pdf", convert)
    monkeypatch.setattr(Message, "reply_document", reply)
    event = message(document=Document(file_id="file", file_unique_id="unique", file_name="file.docx"))
    await viewer.view(event, Settings())
    assert source.closed
    assert convert.call_args.args[2] == "application/octet-stream"
    assert reply.call_args.args[0].filename == "file.pdf"
    assert reply.call_args.kwargs["thumbnail"].filename == "thumbnail.jpg"


async def test_log_middleware_never_records_update_text_names_or_ids(caplog):
    caplog.set_level("DEBUG", logger="hub_bot.dispatch")
    await LoggingMiddleware()(AsyncMock(return_value=None), Update(update_id=123456789, message=message()), {})
    record = next(record for record in caplog.records if record.name == "hub_bot.dispatch")
    assert record.outcome == "handled"
    assert "private canary text" not in repr(record.__dict__)
    assert "Synthetic" not in repr(record.__dict__)
    assert "123456789" not in repr(record.__dict__)


async def test_membership_side_effects_continue_to_handler_without_ambient_bot(monkeypatch):
    bot = SimpleNamespace(id=123, send_message=AsyncMock(), get_chat_member_count=AsyncMock(return_value=5))
    middleware = EventsMiddleware(bot, SimpleNamespace(list_directory=AsyncMock(return_value=[])), 999)
    monkeypatch.setattr("hub_bot.events.chat_link", AsyncMock(return_value="Synthetic chat"))
    handler = AsyncMock(return_value="handled")
    event = message(from_user=None, new_chat_members=[User(id=123, is_bot=True, first_name="Bot")])
    assert await middleware(handler, event, {}) == "handled"
    bot.send_message.assert_awaited_once()
    handler.assert_awaited_once()


async def test_pin_update_waits_for_other_api_calls_after_one_rejected_edit(monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def edit(text, chat_id, message_id):
        if chat_id == 1:
            raise TelegramBadRequest(method=EditMessageText(chat_id=1, message_id=1, text="synthetic"), message="not modified")
        entered.set()
        await finish.wait()

    manager = EcosystemManager(SimpleNamespace(edit_message_text=edit), SimpleNamespace())
    manager.update_ic_members = AsyncMock()
    manager.text = AsyncMock(return_value="synthetic")
    rows = {index: SimpleNamespace(chat_id=index, pinned_message_id=1) for index in (1, 2)}
    manager.db = SimpleNamespace(list_directory=AsyncMock(return_value=list(rows.values())))
    task = asyncio.create_task(manager.update_pins())
    await entered.wait()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    await task


async def test_pin_edit_targets_real_bot_api_fields_and_forced_refresh_bypasses_throttle():
    session = RecordingSession()
    bot = Bot("123456789:" + "a" * 35, session=session)
    entry = SimpleNamespace(chat_id=-1001, pinned_message_id=7)
    repository = SimpleNamespace(list_directory=AsyncMock(return_value=[entry]))
    manager = EcosystemManager(bot, repository)
    manager.update_ic_members = AsyncMock()
    manager.text = AsyncMock(return_value="Synthetic links")
    try:
        await manager.update_pins()
        await manager.update_pins()
        assert len(session.methods) == 1
        await manager.update_pins(forced=True)
        assert len(session.methods) == 2
        assert all(method.chat_id == -1001 and method.message_id == 7 for method in session.methods)
        assert all(method.business_connection_id is None for method in session.methods)
    finally:
        await session.close()


async def test_directory_cache_is_local_and_invalidated_after_mutation():
    first = SimpleNamespace(chat_id=-1001)
    second = SimpleNamespace(chat_id=-1002)
    repository = SimpleNamespace(list_directory=AsyncMock(side_effect=[[first], [second]]))
    manager = EcosystemManager(SimpleNamespace(), repository)
    assert set(await manager.directory()) == {-1001}
    assert set(await manager.directory()) == {-1001}
    repository.list_directory.assert_awaited_once()
    manager.invalidate_directory()
    assert set(await manager.directory()) == {-1002}
    other = EcosystemManager(SimpleNamespace(), SimpleNamespace(list_directory=AsyncMock(return_value=[])))
    assert await other.directory() == {}

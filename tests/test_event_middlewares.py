import asyncio
import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import EditMessageText, GetChat, GetChatMemberCount
from aiogram.types import Chat, ChatFullInfo, Document, Message, Update, User

from msu_hub_bot.telegram.middlewares.check_gets import CheckGets
from msu_hub_bot.telegram.middlewares.logs import LoggingMiddleware
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.middlewares.skip777000 import Skip777000
from msu_hub_bot.telegram.middlewares.updates import UpdatesMiddleware
from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware
from msu_hub_bot.telegram.runtime import AdmissionMiddleware, Supervisor
from msu_hub_bot.events import EcosystemManager, EventsMiddleware
from msu_hub_bot.storage.models import DirectoryRecord
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


async def test_unhandled_reaction_uses_the_same_single_owned_archive_write():
    supervisor = Supervisor()
    db = SimpleNamespace(archive_update=AsyncMock())
    middleware = UpdatesMiddleware(db, supervisor)
    update = Update.model_validate(
        {
            "update_id": 12,
            "message_reaction": {
                "chat": {"id": -1001, "type": "supergroup"},
                "message_id": 42,
                "date": 1_700_000_000,
                "user": {"id": 10, "is_bot": False, "first_name": "Synthetic"},
                "old_reaction": [{"type": "emoji", "emoji": "👍"}],
                "new_reaction": [],
            },
        }
    )
    result = await asyncio.create_task(dispatch_archive(middleware, supervisor, AsyncMock(return_value=UNHANDLED), update))
    assert result is UNHANDLED
    drained = await supervisor.drain(1, cancel_timeout=0.1)
    assert drained.failed_jobs == 0
    db.archive_update.assert_awaited_once()
    row = db.archive_update.call_args.args[0]
    assert row.handled is False and row.kind == "message_reaction"
    assert row.reaction.user_id == 10 and row.reaction.reactions == []
    assert row.reaction.previous_active is True
    assert row.messages == []


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
    from msu_hub_bot.providers.pdf import PdfDocument

    converted = PdfDocument(b"%PDF-synthetic", filename="file.pdf")
    convert = AsyncMock(return_value=converted)
    reply = AsyncMock()
    monkeypatch.setattr("msu_hub_bot.telegram.middlewares.viewer.download", AsyncMock(return_value=source))
    monkeypatch.setattr("msu_hub_bot.telegram.middlewares.viewer.bot_for", lambda value: SimpleNamespace())
    monkeypatch.setattr("msu_hub_bot.telegram.middlewares.viewer.convert_to_pdf", convert)
    monkeypatch.setattr(Message, "reply_document", reply)
    event = message(document=Document(file_id="file", file_unique_id="unique", file_name="file.docx"))
    await viewer.view(event, Settings())
    assert source.closed
    assert convert.call_args.args[2] == "application/octet-stream"
    assert reply.call_args.args[0].filename == "file.pdf"
    assert reply.call_args.args[0].data == b"%PDF-synthetic"
    assert converted.closed and "thumbnail" not in reply.call_args.kwargs


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
    monkeypatch.setattr("msu_hub_bot.events.chat_link", AsyncMock(return_value="Synthetic chat"))
    handler = AsyncMock(return_value="handled")
    event = message(from_user=None, new_chat_members=[User(id=123, is_bot=True, first_name="Bot")])
    assert await middleware(handler, event, {}) == "handled"
    bot.send_message.assert_awaited_once()
    handler.assert_awaited_once()


async def test_pin_update_waits_for_other_api_calls_after_one_rejected_edit(monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def edit(text, chat_id, message_id):
        if chat_id == 1:
            raise TelegramBadRequest(method=EditMessageText(chat_id=1, message_id=1, text="synthetic"), message="message is not modified")
        entered.set()
        await finish.wait()

    manager = EcosystemManager(SimpleNamespace(edit_message_text=edit), SimpleNamespace())
    manager.update_ic_members = AsyncMock()
    manager.get_chat = AsyncMock(return_value=SimpleNamespace())
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
    manager.get_chat = AsyncMock(return_value=SimpleNamespace())
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


def ecosystem_chat(chat_id):
    return ChatFullInfo(
        id=chat_id,
        type="supergroup",
        title="Synthetic directory chat",
        username=f"synthetic_{abs(chat_id)}",
        accent_color_id=1,
        max_reaction_count=1,
        accepted_gift_types=dict.fromkeys(
            ("unlimited_gifts", "limited_gifts", "unique_gifts", "premium_subscription", "gifts_from_channels"), False
        ),
    )


@pytest.fixture
def ecosystem(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr("msu_hub_bot.events.monotonic", lambda: clock.now)
    entries = {
        chat_id: DirectoryRecord(
            id=UUID(int=abs(chat_id)),
            created=datetime(2026, 1, 1, tzinfo=UTC),
            chat_id=chat_id,
            name=f"Synthetic {abs(chat_id)}",
            section="other",
            is_hidden=False,
            members=5,
            pinned_message_id=7,
        )
        for chat_id in (-101, -102)
    }

    async def patch(chat_id, changes):
        entries[chat_id] = entries[chat_id].model_copy(update=changes.model_dump(exclude_unset=True))
        return entries[chat_id]

    repository = SimpleNamespace(
        list_directory=AsyncMock(side_effect=lambda: list(entries.values())),
        patch_directory=AsyncMock(side_effect=patch),
        delete_directory=AsyncMock(),
    )
    bot = SimpleNamespace(
        id=123,
        get_chat=AsyncMock(side_effect=ecosystem_chat),
        get_chat_member_count=AsyncMock(side_effect=lambda chat_id: 5 if chat_id == -101 else 8),
        edit_message_text=AsyncMock(),
        delete_message=AsyncMock(),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(manager=EcosystemManager(bot, repository), bot=bot, repository=repository, entries=entries, clock=clock)


@pytest.mark.parametrize("stage", ["lookup", "members"])
@pytest.mark.parametrize("alias", [None, "synthetic_alias"])
async def test_unavailable_directory_chat_preserves_data_and_other_pins(ecosystem, stage, alias):
    rig = ecosystem
    rig.entries[-101] = rig.entries[-101].model_copy(update={"username_alias": alias})
    original = rig.entries[-101].model_dump()

    async def lookup(chat_id):
        if chat_id == -101:
            raise TelegramBadRequest(method=GetChat(chat_id=chat_id), message="Bad Request: chat not found")
        return ecosystem_chat(chat_id)

    async def members(chat_id):
        if chat_id == -101:
            raise TelegramForbiddenError(
                method=GetChatMemberCount(chat_id=chat_id), message="Forbidden: bot is not a member of the supergroup chat"
            )
        return 8

    if stage == "lookup":
        rig.bot.get_chat.side_effect = lookup
    else:
        rig.bot.get_chat_member_count.side_effect = members
    await rig.manager.update_pins()
    assert rig.entries[-101].model_dump() == original
    rig.repository.delete_directory.assert_not_awaited()
    assert rig.repository.patch_directory.await_count == 1
    assert rig.repository.patch_directory.call_args.args[0] == -102
    assert rig.repository.patch_directory.call_args.args[1].model_dump(exclude_unset=True) == {"members": 8}
    rig.bot.edit_message_text.assert_awaited_once()
    assert rig.bot.edit_message_text.call_args.kwargs == {"chat_id": -102, "message_id": 7}
    text = rig.bot.edit_message_text.call_args.args[0]
    assert "Synthetic 101" in text and "5 уч." in text
    assert ("@synthetic_alias" if alias else "временно недоступен") in text
    assert "@synthetic_102" in text
    assert await rig.manager.link(-101) == ("@synthetic_alias" if alias else "временно недоступен")
    assert await rig.manager.pin(-101) is False
    assert rig.bot.get_chat.await_count == 2
    rig.bot.send_message.assert_not_awaited()
    rig.bot.delete_message.assert_not_awaited()


async def test_unavailable_chat_cooldown_expires_and_cache_is_bounded(ecosystem):
    rig = ecosystem
    error = TelegramBadRequest(method=GetChat(chat_id=-101), message="chat not found")
    rig.bot.get_chat.side_effect = [error, ecosystem_chat(-101)]
    assert await rig.manager.get_chat(-101) is None
    rig.clock.now += 599
    assert await rig.manager.get_chat(-101) is None
    assert rig.bot.get_chat.await_count == 1
    rig.clock.now += 1
    assert (await rig.manager.get_chat(-101)).id == -101
    assert rig.bot.get_chat.await_count == 2
    assert rig.manager._chats.maxsize == 1024


async def test_forced_pin_refresh_retries_negative_chat_cache(ecosystem):
    rig = ecosystem
    rig.bot.get_chat.side_effect = TelegramBadRequest(method=GetChat(chat_id=-101), message="chat not found")
    await rig.manager.update_pins()
    rig.bot.edit_message_text.assert_not_awaited()
    assert rig.bot.get_chat.await_count == 2
    rig.bot.get_chat.side_effect = ecosystem_chat
    await rig.manager.update_pins()
    assert rig.bot.get_chat.await_count == 2
    await rig.manager.update_pins(forced=True)
    assert rig.bot.get_chat.await_count == 4
    assert rig.bot.edit_message_text.await_count == 2


@pytest.mark.parametrize("stage", ["lookup", "members", "edit"])
@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, asyncio.CancelledError])
async def test_unrelated_directory_errors_propagate_without_negative_caching(ecosystem, stage, error_type):
    rig = ecosystem
    error = (
        asyncio.CancelledError()
        if error_type is asyncio.CancelledError
        else error_type(method=GetChat(chat_id=-101), message="Synthetic unrelated failure")
    )
    target = {"lookup": rig.bot.get_chat, "members": rig.bot.get_chat_member_count, "edit": rig.bot.edit_message_text}[stage]
    target.side_effect = error
    with pytest.raises(error_type):
        await rig.manager.update_pins()
    assert all(chat is not None for chat in rig.manager._chats.values())
    rig.repository.delete_directory.assert_not_awaited()


async def test_directory_storage_failure_remains_visible(ecosystem):
    rig = ecosystem
    rig.repository.patch_directory.side_effect = RuntimeError("Synthetic storage outage")
    with pytest.raises(RuntimeError, match="Synthetic storage outage"):
        await rig.manager.update_pins()
    rig.bot.edit_message_text.assert_not_awaited()
    assert await rig.manager.get_chat(-102) is not None


async def test_chat_becoming_inaccessible_during_edit_does_not_block_other_pins(ecosystem):
    rig = ecosystem

    async def edit(text, chat_id, message_id):
        if chat_id == -101:
            raise TelegramForbiddenError(
                method=EditMessageText(chat_id=chat_id, message_id=message_id, text=text),
                message="Forbidden: bot was kicked from the supergroup chat",
            )

    rig.bot.edit_message_text.side_effect = edit
    await rig.manager.update_pins()
    assert rig.bot.edit_message_text.await_count == 2
    assert await rig.manager.get_chat(-101) is None
    assert await rig.manager.get_chat(-102) is not None
    assert rig.entries[-101].pinned_message_id == 7


async def test_membership_event_with_stale_directory_chat_still_reaches_handler(ecosystem, monkeypatch):
    rig = ecosystem

    async def lookup(chat_id):
        if chat_id == -101:
            raise TelegramBadRequest(method=GetChat(chat_id=chat_id), message="chat not found")
        return ecosystem_chat(chat_id)

    rig.bot.get_chat.side_effect = lookup
    monkeypatch.setattr("msu_hub_bot.events.chat_link", AsyncMock(return_value="Synthetic chat"))
    middleware = EventsMiddleware(rig.bot, rig.repository, 0, em=rig.manager)
    event = message(
        chat=Chat(id=-102, type="supergroup", title="Synthetic"), new_chat_members=[User(id=42, is_bot=False, first_name="Test")]
    )
    handler = AsyncMock(return_value="handled")
    assert await middleware(handler, event, {}) == "handled"
    handler.assert_awaited_once()
    rig.bot.edit_message_text.assert_awaited_once()


async def test_automatic_pdf_unknown_size_stops_streaming_without_reply(monkeypatch):
    from msu_hub_bot.telegram import files
    from msu_hub_bot.telegram.middlewares import viewer as module
    from telegram_helpers import make_bot, make_message

    bot = make_bot()
    bot.session.download_bytes = b"123456789"
    viewer = ViewerMiddleware(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    event = make_message(bot, document=dict(file_id="file", file_unique_id="unique", file_name="file.docx"))
    monkeypatch.setattr(module, "MAX_DOWNLOAD_BYTES", 8)
    convert = AsyncMock()
    monkeypatch.setattr(module, "convert_to_pdf", convert)
    streams = []
    bounded = files._BoundedDownload

    def capture(limit):
        stream = bounded(limit)
        streams.append(stream)
        return stream

    monkeypatch.setattr(files, "_BoundedDownload", capture)
    await viewer.view(event, Settings())
    convert.assert_not_awaited()
    assert len(streams) == 1 and streams[0].closed
    assert not any(method.__api_method__.startswith("send") for method in bot.session.methods)

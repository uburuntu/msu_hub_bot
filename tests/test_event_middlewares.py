import asyncio
import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import EditMessageText, GetChat, GetChatMemberCount, UnpinChatMessage
from aiogram.types import Chat, ChatFullInfo, ChatMemberUpdated, Document, Message, Update, User

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
    monkeypatch.setattr(middleware.em, "chat_link", AsyncMock(return_value="Synthetic chat"))
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

    async def clear_pin(chat_id, expected_message_id):
        current = entries.get(chat_id)
        if current is not None and current.pinned_message_id == expected_message_id:
            entries[chat_id] = current.model_copy(update={"pinned_message_id": None})
        return entries.get(chat_id)

    repository = SimpleNamespace(
        list_directory=AsyncMock(side_effect=lambda: list(entries.values())),
        patch_directory=AsyncMock(side_effect=patch),
        clear_directory_pin=AsyncMock(side_effect=clear_pin),
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


@pytest.mark.parametrize("events_chat_id", [0, 999])
async def test_ordinary_directory_messages_do_not_fetch_metadata_or_run_maintenance(ecosystem, events_chat_id):
    rig = ecosystem
    middleware = EventsMiddleware(rig.bot, rig.repository, events_chat_id, em=rig.manager)
    handler = AsyncMock(return_value="handled")
    event = message(chat=Chat(id=-101, type="supergroup", title="Synthetic"))
    assert await middleware(handler, event, {}) == "handled"
    rig.repository.list_directory.assert_not_awaited()
    rig.bot.get_chat.assert_not_awaited()
    rig.bot.get_chat_member_count.assert_not_awaited()
    rig.bot.edit_message_text.assert_not_awaited()
    handler.assert_awaited_once()


async def test_disabled_event_notifications_skip_metadata_but_keep_membership_maintenance(ecosystem):
    rig = ecosystem
    middleware = EventsMiddleware(rig.bot, rig.repository, 0, em=rig.manager)
    rig.manager.update_pins = AsyncMock()
    handler = AsyncMock()
    await middleware(handler, message(new_chat_title="New title"), {})
    rig.repository.list_directory.assert_not_awaited()
    await middleware(
        handler,
        message(chat=Chat(id=-101, type="supergroup", title="Synthetic"), new_chat_members=[User(id=123, is_bot=True, first_name="Bot")]),
        {},
    )
    rig.manager.update_pins.assert_awaited_once()
    rig.bot.get_chat.assert_not_awaited()
    rig.bot.get_chat_member_count.assert_not_awaited()
    rig.bot.send_message.assert_not_awaited()


async def test_event_links_share_cached_metadata_without_losing_new_titles(ecosystem):
    rig = ecosystem
    middleware = EventsMiddleware(rig.bot, rig.repository, 999, em=rig.manager)
    handler = AsyncMock()
    for title in ("First title", "Changed title"):
        await middleware(handler, message(chat=Chat(id=-101, type="supergroup", title=title), new_chat_title=title), {})
    rig.bot.get_chat.assert_awaited_once_with(-101)
    assert "Changed title" in rig.bot.send_message.call_args.args[1]
    assert "https://t.me/synthetic_101" in rig.bot.send_message.call_args.args[1]
    assert handler.await_count == 2


async def test_concurrent_chat_cache_misses_share_one_bounded_lookup(ecosystem):
    rig = ecosystem
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(chat_id):
        entered.set()
        await release.wait()
        return ecosystem_chat(chat_id)

    rig.bot.get_chat.side_effect = lookup
    tasks = [asyncio.create_task(rig.manager.get_chat(-101)) for _ in range(8)]
    await entered.wait()
    await asyncio.sleep(0)
    rig.bot.get_chat.assert_awaited_once()
    release.set()
    result = await asyncio.gather(*tasks)
    assert all(chat.id == -101 for chat in result)
    rig.bot.get_chat.assert_awaited_once()


async def test_membership_change_during_lookup_does_not_cache_stale_access(ecosystem):
    rig = ecosystem
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(chat_id):
        entered.set()
        await release.wait()
        raise TelegramForbiddenError(GetChat(chat_id=chat_id), "Forbidden: bot was kicked from the supergroup chat")

    rig.bot.get_chat.side_effect = lookup
    task = asyncio.create_task(rig.manager.get_chat(-101))
    await entered.wait()
    middleware = EventsMiddleware(rig.bot, rig.repository, 0, em=rig.manager)
    await middleware(AsyncMock(), bot_rights_update(), {})
    release.set()
    assert await task is None
    assert -101 not in rig.manager._chats
    rig.bot.get_chat.side_effect = ecosystem_chat
    assert (await rig.manager.get_chat(-101)).id == -101


async def test_directory_refresh_bounds_active_telegram_requests(ecosystem):
    rig = ecosystem
    rig.entries.update({index: rig.entries[-101].model_copy(update={"chat_id": index}) for index in range(-110, -102)})
    for chat_id in rig.entries:
        rig.manager._chats[chat_id] = ecosystem_chat(chat_id)
    active = 0
    peak = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def members(chat_id):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        if active == 3:
            entered.set()
        await release.wait()
        active -= 1
        return 5

    rig.bot.get_chat_member_count.side_effect = members
    task = asyncio.create_task(rig.manager.update_pins())
    await entered.wait()
    await asyncio.sleep(0)
    assert active == 3
    release.set()
    await task
    assert peak == 3 and rig.bot.get_chat_member_count.await_count == len(rig.entries)


@pytest.mark.parametrize("stage", ["lookup", "members", "edit"])
async def test_rate_limited_maintenance_continues_dispatch_and_honors_server_cooldown(ecosystem, stage):
    rig = ecosystem
    target = {"lookup": rig.bot.get_chat, "members": rig.bot.get_chat_member_count, "edit": rig.bot.edit_message_text}[stage]
    target.side_effect = TelegramRetryAfter(GetChat(chat_id=-101), "Synthetic flood wait", retry_after=31)
    middleware = EventsMiddleware(rig.bot, rig.repository, 0, em=rig.manager)
    handler = AsyncMock(return_value="handled")
    event = message(
        chat=Chat(id=-101, type="supergroup", title="Synthetic"), new_chat_members=[User(id=42, is_bot=False, first_name="Test")]
    )
    assert await middleware(handler, event, {}) == "handled"
    assert rig.manager.throttled
    calls = target.await_count
    assert calls >= 1
    await rig.manager.update_pins(forced=True)
    assert await rig.manager.pin(-101, forced=True) is False
    await middleware(handler, event, {})
    assert target.await_count == calls
    handler.assert_awaited()
    assert all(chat is not None for chat in rig.manager._chats.values())
    rig.repository.clear_directory_pin.assert_not_awaited()
    target.side_effect = ecosystem_chat if stage == "lookup" else (lambda chat_id: 5) if stage == "members" else None
    rig.clock.now += 31
    await rig.manager.update_pins()
    assert not rig.manager.throttled and target.await_count > calls


async def test_overlapping_automatic_refresh_does_not_wait_for_running_refresh(ecosystem):
    rig = ecosystem
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh():
        entered.set()
        await release.wait()

    rig.manager._update_pins = AsyncMock(side_effect=refresh)
    task = asyncio.create_task(rig.manager.update_pins())
    await entered.wait()
    await asyncio.wait_for(rig.manager.update_pins(), timeout=0.1)
    rig.manager._update_pins.assert_awaited_once()
    release.set()
    await task


async def test_confirmed_missing_pin_is_cleared_once_without_replacing_messages(ecosystem):
    rig = ecosystem
    before = rig.entries[-101].model_dump()

    async def edit(text, chat_id, message_id):
        if chat_id == -101:
            raise TelegramBadRequest(EditMessageText(chat_id=chat_id, message_id=message_id, text=text), "message to edit not found")

    rig.bot.edit_message_text.side_effect = edit
    await rig.manager.update_pins()
    assert rig.entries[-101].model_dump() == before | {"pinned_message_id": None}
    rig.repository.clear_directory_pin.assert_awaited_once_with(-101, 7)
    rig.clock.now += 601
    await rig.manager.update_pins()
    assert [call.kwargs["chat_id"] for call in rig.bot.edit_message_text.await_args_list].count(-101) == 1
    rig.bot.send_message.assert_not_awaited()
    rig.bot.delete_message.assert_not_awaited()
    rig.repository.delete_directory.assert_not_awaited()


async def test_stale_edit_cannot_clear_a_concurrent_pin_replacement(ecosystem):
    rig = ecosystem

    async def edit(text, chat_id, message_id):
        if chat_id == -101 and message_id == 7:
            rig.entries[chat_id] = rig.entries[chat_id].model_copy(update={"pinned_message_id": 99})
            raise TelegramBadRequest(EditMessageText(chat_id=chat_id, message_id=message_id, text=text), "message to edit not found")

    rig.bot.edit_message_text.side_effect = edit
    await rig.manager.update_pins()
    rig.repository.clear_directory_pin.assert_awaited_once_with(-101, 7)
    assert rig.entries[-101].pinned_message_id == 99
    rig.clock.now += 601
    await rig.manager.update_pins()
    assert any(call.kwargs == {"chat_id": -101, "message_id": 99} for call in rig.bot.edit_message_text.await_args_list)


def bot_rights_update(chat_id=-101, user_id=123):
    member = {
        "status": "administrator",
        "user": {"id": user_id, "is_bot": True, "first_name": "Synthetic bot"},
        "is_anonymous": False,
        **dict.fromkeys(
            (
                "can_be_edited",
                "can_manage_chat",
                "can_delete_messages",
                "can_manage_video_chats",
                "can_restrict_members",
                "can_promote_members",
                "can_change_info",
                "can_invite_users",
                "can_post_stories",
                "can_edit_stories",
                "can_delete_stories",
                "can_send_welcome_messages",
            ),
            False,
        ),
    }
    return ChatMemberUpdated.model_validate(
        {
            "chat": {"id": chat_id, "type": "supergroup", "title": "Synthetic"},
            "from": {"id": 42, "is_bot": False, "first_name": "Synthetic"},
            "date": 1_700_000_000,
            "old_chat_member": member | {"can_pin_messages": False},
            "new_chat_member": member | {"can_pin_messages": True},
        }
    )


@pytest.mark.parametrize("recovery", ["expiry", "membership", "forced"])
async def test_uneditable_pin_cooldown_preserves_id_and_recovers(ecosystem, recovery):
    rig = ecosystem

    async def edit(text, chat_id, message_id):
        if chat_id == -101:
            raise TelegramBadRequest(EditMessageText(chat_id=chat_id, message_id=message_id, text=text), "message can't be edited")

    rig.bot.edit_message_text.side_effect = edit
    await rig.manager.update_pins()
    rig.clock.now += 601
    await rig.manager.update_pins()
    assert [call.kwargs["chat_id"] for call in rig.bot.edit_message_text.await_args_list].count(-101) == 1
    assert rig.entries[-101].pinned_message_id == 7
    rig.repository.clear_directory_pin.assert_not_awaited()
    if recovery == "expiry":
        rig.clock.now += 3000
    elif recovery == "membership":
        middleware = EventsMiddleware(rig.bot, rig.repository, 0, em=rig.manager)
        await middleware(AsyncMock(), bot_rights_update(), {})
        assert -101 not in rig.manager._chats
    rig.bot.edit_message_text.side_effect = None
    await rig.manager.update_pins(forced=recovery == "forced")
    assert [call.kwargs["chat_id"] for call in rig.bot.edit_message_text.await_args_list].count(-101) == 2


@pytest.mark.parametrize("recovery", ["expiry", "membership"])
async def test_automatic_unpin_permission_cooldown_recovers(monkeypatch, recovery):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr("msu_hub_bot.telegram.middlewares.skip777000.monotonic", lambda: clock.now)
    unpin = AsyncMock(
        side_effect=[
            TelegramBadRequest(UnpinChatMessage(chat_id=-101, message_id=1), "Not enough rights to manage pinned messages in the chat"),
            True,
        ]
    )
    monkeypatch.setattr(Message, "unpin", unpin)
    middleware = Skip777000(bot_id=123)
    handler = AsyncMock()
    event = message(chat=Chat(id=-101, type="supergroup", title="Synthetic"), is_automatic_forward=True)
    await middleware(handler, event, {})
    await middleware(handler, event.model_copy(update={"message_id": 2}), {})
    unpin.assert_awaited_once()
    handler.assert_not_awaited()
    if recovery == "expiry":
        clock.now += 300
    else:
        await middleware(handler, bot_rights_update(user_id=999), {})
        await middleware(handler, event, {})
        unpin.assert_awaited_once()
        await middleware(handler, bot_rights_update(), {})
    await middleware(handler, event.model_copy(update={"message_id": 3}), {})
    assert unpin.await_count == 2


async def test_automatic_unpin_missing_message_does_not_disable_other_messages(monkeypatch):
    unpin = AsyncMock(side_effect=[TelegramBadRequest(UnpinChatMessage(chat_id=-101, message_id=1), "message to unpin not found"), True])
    monkeypatch.setattr(Message, "unpin", unpin)
    middleware = Skip777000()
    handler = AsyncMock()
    event = message(is_automatic_forward=True)
    await middleware(handler, event, {})
    await middleware(handler, event, {})
    await middleware(handler, event.model_copy(update={"message_id": 2}), {})
    assert unpin.await_count == 2
    handler.assert_not_awaited()


async def test_automatic_unpin_targets_exact_forward_without_permissions_lookup():
    session = RecordingSession()
    bot = Bot("123456789:" + "a" * 35, session=session)
    event = message(is_automatic_forward=True, message_id=77, business_connection_id="synthetic-business").as_(bot)
    try:
        await Skip777000(bot_id=bot.id)(AsyncMock(), event, {})
        [method] = session.methods
        assert isinstance(method, UnpinChatMessage)
        assert (method.chat_id, method.message_id, method.business_connection_id) == (event.chat.id, 77, "synthetic-business")
    finally:
        await session.close()


@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, asyncio.CancelledError])
async def test_automatic_unpin_does_not_silence_unrelated_errors(monkeypatch, error_type):
    error = (
        asyncio.CancelledError()
        if error_type is asyncio.CancelledError
        else error_type(UnpinChatMessage(chat_id=-101), "Synthetic unrelated failure")
    )
    monkeypatch.setattr(Message, "unpin", AsyncMock(side_effect=error))
    with pytest.raises(error_type):
        await Skip777000()(AsyncMock(), message(is_automatic_forward=True), {})


async def test_unpin_rate_limit_defers_retries_and_does_not_block_ordinary_messages(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr("msu_hub_bot.telegram.middlewares.skip777000.monotonic", lambda: clock.now)
    unpin = AsyncMock(side_effect=[TelegramRetryAfter(UnpinChatMessage(chat_id=-101), "Synthetic rate limit", retry_after=31), True])
    monkeypatch.setattr(Message, "unpin", unpin)
    middleware = Skip777000(bot_id=123)
    handler = AsyncMock(return_value="handled")
    event = message(is_automatic_forward=True)
    await middleware(handler, event, {})
    await middleware(handler, bot_rights_update(), {})
    await middleware(handler, event.model_copy(update={"message_id": 2}), {})
    unpin.assert_awaited_once()
    assert await middleware(handler, message(text="/help"), {}) == "handled"
    clock.now += 31
    await middleware(handler, event.model_copy(update={"message_id": 3}), {})
    assert unpin.await_count == 2


async def test_rights_update_during_unpin_does_not_reintroduce_old_permission_cooldown(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def unpin(_message):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await release.wait()
            raise TelegramBadRequest(UnpinChatMessage(chat_id=-101), "Not enough rights to manage pinned messages in the chat")

    monkeypatch.setattr(Message, "unpin", unpin)
    middleware = Skip777000(bot_id=123)
    event = message(chat=Chat(id=-101, type="supergroup", title="Synthetic"), is_automatic_forward=True)
    task = asyncio.create_task(middleware(AsyncMock(), event, {}))
    await entered.wait()
    await middleware(AsyncMock(), bot_rights_update(), {})
    release.set()
    await task
    await middleware(AsyncMock(), event.model_copy(update={"message_id": 2}), {})
    assert attempts == 2


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

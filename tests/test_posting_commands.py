import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import CopyMessage, SendMessage
from aiogram.types import Chat, Message, MessageOriginChannel, MessageOriginChat, Update, User

from common.tg.state import ReleasableEventIsolation, StateContextMiddleware, UpdateStateContext
from hub_bot.commands import posting
from hub_bot.commands.control import process_cancel

KEY = StorageKey(bot_id=123456, chat_id=-10012345, user_id=10, thread_id=2)


def message(text="test", **values):
    return Message(
        message_id=values.pop("message_id", 1),
        date=1_700_000_000,
        chat=Chat(id=KEY.chat_id, type="supergroup", is_forum=True),
        from_user=User(id=KEY.user_id, is_bot=False, first_name="Synthetic"),
        message_thread_id=2,
        is_topic_message=True,
        text=text,
        **values,
    )


@pytest.fixture
def replies(monkeypatch):
    calls = []

    async def reply(self, text, **kwargs):
        calls.append((self.message_id, text, kwargs))
        return self

    monkeypatch.setattr(Message, "reply", reply)
    monkeypatch.setattr(Message, "answer", reply)
    return calls


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), KEY)


async def test_posting_requires_source_reply_without_creating_draft(state, replies):
    await posting.MakePost.process(message("/make_post"), state, 0)
    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert "реплаем" in replies[0][1]


async def test_posting_persists_draft_and_native_keyboard(state, replies):
    original = message("source", message_id=22)
    await posting.MakePost.process(message("/make_post", reply_to_message=original), state, -100777)
    assert await state.get_state() == posting.MakePostStates.destination.state
    assert await state.get_data() == {"post_chat_id": KEY.chat_id, "post_message_id": 22, "dest_chat_ids": []}
    assert "-100777" in replies[0][1]
    bot = SimpleNamespace(get_chat=AsyncMock(return_value=Chat(id=-100777, type="channel", title="A < B")))
    await posting.MakePost.process_destination(message("@example"), state, bot)
    assert await state.get_state() == posting.MakePostStates.waiting.state
    assert (await state.get_data())["dest_chat_ids"] == [-100777]
    assert "A &lt; B" in replies[-1][1]
    assert [row[0].text for row in replies[-1][2]["reply_markup"].keyboard] == [
        "Запустить рассылку",
        "Добавить целевой чат",
    ]
    await posting.MakePost.process_waiting(
        message("Добавить целевой чат"),
        state,
        bot,
        UpdateStateContext(True),
        -100777,
    )
    assert await state.get_state() == posting.MakePostStates.destination.state
    assert (await state.get_data())["dest_chat_ids"] == [-100777]


@pytest.mark.parametrize("origin_type", [MessageOriginChannel, MessageOriginChat])
async def test_posting_recognizes_native_forward_origins(origin_type, state, replies):
    await state.set_data(posting.PostDraft(post_chat_id=KEY.chat_id, post_message_id=22).model_dump())
    chat = Chat(id=-100888, type="channel", title="Synthetic channel")
    if origin_type is MessageOriginChannel:
        origin = origin_type(date=1_700_000_000, chat=chat, message_id=99)
    else:
        origin = origin_type(date=1_700_000_000, sender_chat=chat)
    bot = SimpleNamespace(get_chat=AsyncMock(return_value=chat))
    await posting.MakePost.process_destination(message(None, forward_origin=origin), state, bot)
    bot.get_chat.assert_awaited_once_with(-100888)
    assert (await state.get_data())["dest_chat_ids"] == [-100888]


async def test_rejected_destination_keeps_draft_without_raw_provider_error(state, replies, capsys):
    draft = posting.PostDraft(post_chat_id=KEY.chat_id, post_message_id=22).model_dump()
    await state.set_data(draft)
    await state.set_state(posting.MakePostStates.destination)
    error = TelegramBadRequest(method=CopyMessage(chat_id=1, from_chat_id=2, message_id=3), message="private-error-canary")
    bot = SimpleNamespace(get_chat=AsyncMock(side_effect=error))
    await posting.MakePost.process_destination(message("@missing"), state, bot)
    assert await state.get_data() == draft
    assert await state.get_state() == posting.MakePostStates.destination.state
    assert "private-error-canary" not in repr(replies) + capsys.readouterr().out


async def test_broadcast_consumes_draft_releases_lock_and_survives_cancel(state, replies):
    await state.set_data(posting.PostDraft(post_chat_id=KEY.chat_id, post_message_id=22, dest_chat_ids=[-100777]).model_dump())
    await state.set_state(posting.MakePostStates.waiting)
    entered, finish = asyncio.Event(), asyncio.Event()
    isolation = ReleasableEventIsolation()

    async def copy_message(*args):
        assert await state.get_data() == {}
        assert await state.get_state() is None
        entered.set()
        await finish.wait()

    bot = SimpleNamespace(copy_message=AsyncMock(side_effect=copy_message))

    async def scoped(event, callback):
        async def run(update, data):
            async with isolation.lock(KEY):
                return await callback(data["state_context"])

        return await StateContextMiddleware()(run, Update(update_id=event.message_id, message=event), {})

    event = message("Запустить рассылку")
    broadcast = asyncio.create_task(scoped(event, lambda context: posting.MakePost.process_waiting(event, state, bot, context, 0)))
    await entered.wait()
    cancel = message("/cancel", message_id=2)
    await asyncio.wait_for(scoped(cancel, lambda context: process_cancel(cancel, state, context)), timeout=1)
    assert not broadcast.done()
    assert replies[-1][0] == 2
    finish.set()
    await broadcast
    assert replies[-1][1] == "Рассылка завершена!"
    assert isolation.key_count == 0
    await isolation.close()


async def test_broadcast_continues_after_one_inaccessible_destination(state, replies):
    await state.set_data(posting.PostDraft(post_chat_id=KEY.chat_id, post_message_id=22, dest_chat_ids=[-100777, -100888]).model_dump())
    error = TelegramBadRequest(method=CopyMessage(chat_id=1, from_chat_id=2, message_id=3), message="private-error-canary")
    bot = SimpleNamespace(copy_message=AsyncMock(side_effect=[error, None]))
    await posting.MakePost.process_run(message(), state, bot, UpdateStateContext(True))
    assert bot.copy_message.await_count == 2
    assert "private-error-canary" not in repr(replies)
    assert replies[-1][1] == "Рассылка завершена!"


@pytest.mark.parametrize("forward", [False, True])
async def test_bulk_posting_preserves_directory_selection_and_copy_method(monkeypatch, forward):
    chats = [
        SimpleNamespace(chat_id=-1, members=55, section="other"),
        SimpleNamespace(chat_id=-2, members=54, section="other"),
        SimpleNamespace(chat_id=-3, members=None, section="other"),
        SimpleNamespace(chat_id=-4, members=99, section="channel"),
        SimpleNamespace(chat_id=-5, members=99, section="other"),
    ]
    monkeypatch.setattr(posting.EcosystemChat, "query", lambda db: SimpleNamespace(get_all=AsyncMock(return_value=chats)))
    bot = AsyncMock()
    source = message("source", message_id=22)
    command = message("/post_all", reply_to_message=source)
    function = posting.process_post_forward_all if forward else posting.process_post_all
    await function(command, bot, object(), -5)
    if forward:
        bot.forward_message.assert_awaited_once_with(-1, KEY.chat_id, 22, disable_notification=True)
        bot.assert_not_awaited()
    else:
        bot.assert_awaited_once()
        method = bot.call_args.args[0]
        assert isinstance(method, SendMessage)
        assert method.chat_id == -1 and method.text == "source" and method.disable_notification
        assert method.parse_mode is None

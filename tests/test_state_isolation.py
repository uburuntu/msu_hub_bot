import asyncio
from contextlib import asynccontextmanager

import pytest
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED, SkipHandler
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Message, Update, User

from common.tg.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
    release_state_isolation,
)

KEY = StorageKey(bot_id=123456, chat_id=-1001234567, user_id=10, thread_id=2)


def message(number=1, *, user=10, topic=2, text="test"):
    return Message(
        message_id=number,
        date=1_700_000_000,
        chat=Chat(id=KEY.chat_id, type="supergroup", is_forum=True),
        from_user=User(id=user, is_bot=False, first_name="Synthetic"),
        message_thread_id=topic,
        is_topic_message=topic is not None,
        text=text,
    )


async def in_state_context(callback):
    async def handler(event, data):
        return await callback(data["state_context"])

    return await StateContextMiddleware()(handler, Update(update_id=1), {})


@asynccontextmanager
async def application():
    bot = Bot("123456:" + "A" * 35)
    dispatcher = Dispatcher(disable_fsm=True)
    storage = MemoryStorage()
    isolation = ReleasableEventIsolation()
    fsm = TopicFSMContextMiddleware(storage, isolation)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.message.middleware(SelectiveIsolationMiddleware())
    dispatcher.callback_query.middleware(SelectiveIsolationMiddleware())
    try:
        yield dispatcher, bot, storage, isolation
    finally:
        await fsm.close()
        await dispatcher.fsm.close()
        await bot.session.close()


async def test_early_release_keeps_waiter_lock_identity_until_every_scope_exits():
    isolation = ReleasableEventIsolation()
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    second_finished = asyncio.Event()
    start_release = asyncio.Event()

    async def first(context):
        async with isolation.lock(KEY):
            first_entered.set()
            await start_release.wait()
            assert release_state_isolation(context)
            assert not release_state_isolation(context)
            await second_finished.wait()
            assert isolation.key_count == 1

    async def second(context):
        async with isolation.lock(KEY):
            second_entered.set()
        second_finished.set()

    one = asyncio.create_task(in_state_context(first))
    await first_entered.wait()
    two = asyncio.create_task(in_state_context(second))
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    start_release.set()
    await asyncio.gather(one, two)
    assert isolation.key_count == 0
    await isolation.close()


async def test_cancelled_waiter_does_not_remove_an_active_key():
    isolation = ReleasableEventIsolation()
    started = asyncio.Event()

    async def waiter():
        started.set()
        async with isolation.lock(KEY):
            pytest.fail("Waiter acquired a held lock")

    async with isolation.lock(KEY):
        task = asyncio.create_task(waiter())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert isolation.key_count == 1
    assert isolation.key_count == 0
    async with isolation.lock(KEY):
        pass
    await isolation.close()


async def test_inherited_child_context_cannot_release_or_replace_parent_scope():
    isolation = ReleasableEventIsolation()

    async def parent(context):
        async with isolation.lock(KEY):
            parent_scope = context.scope

            async def child():
                with pytest.raises(RuntimeError, match="acquiring task"):
                    release_state_isolation(context)
                other = StorageKey(bot_id=123456, chat_id=KEY.chat_id, user_id=11)
                async with isolation.lock(other):
                    assert context.scope is parent_scope

            await asyncio.create_task(child())
            assert context.scope is parent_scope
            assert not parent_scope.released

    await in_state_context(parent)
    assert isolation.key_count == 0
    await isolation.close()


async def test_close_waits_for_scopes_and_rejects_new_admission():
    isolation = ReleasableEventIsolation()
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def worker():
        async with isolation.lock(KEY):
            entered.set()
            await finish.wait()

    task = asyncio.create_task(worker())
    await entered.wait()
    close = asyncio.create_task(isolation.close())
    await asyncio.sleep(0)
    assert not close.done()
    with pytest.raises(RuntimeError, match="closed"):
        async with isolation.lock(KEY):
            pass
    finish.set()
    await task
    await close
    await isolation.close()


async def test_scope_holder_cannot_close_itself():
    isolation = ReleasableEventIsolation()
    async with isolation.lock(KEY):
        with pytest.raises(RuntimeError, match="active state scope"):
            await isolation.close()
    await isolation.close()


async def test_released_scope_can_reacquire_for_a_short_completion_transition():
    isolation = ReleasableEventIsolation()

    async def handler(context):
        async with isolation.lock(KEY):
            original = context.scope
            assert release_state_isolation(context)
            async with isolation.lock(KEY):
                assert context.scope is not original
                assert not context.scope.released
            assert context.scope is original
            assert original.released

    await in_state_context(handler)
    assert isolation.key_count == 0
    await isolation.close()


async def test_real_fsm_close_waits_for_isolation_before_closing_storage():
    closed = []

    class Storage(MemoryStorage):
        async def close(self):
            closed.append(True)

    isolation = ReleasableEventIsolation()
    middleware = TopicFSMContextMiddleware(Storage(), isolation)
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def worker():
        async with isolation.lock(KEY):
            entered.set()
            await finish.wait()
            assert not closed

    task = asyncio.create_task(worker())
    await entered.wait()
    close = asyncio.create_task(middleware.close())
    await asyncio.sleep(0)
    assert not closed
    finish.set()
    await task
    await close
    await middleware.close()
    assert closed == [True]


async def test_default_dispatcher_shutdown_does_not_close_real_fsm_storage():
    closed = []

    class Storage(MemoryStorage):
        async def close(self):
            closed.append(True)

    dispatcher = Dispatcher(disable_fsm=True)
    storage = Storage()
    real_fsm = TopicFSMContextMiddleware(storage, ReleasableEventIsolation())
    dispatcher.update.outer_middleware(StateContextMiddleware())
    dispatcher.update.outer_middleware(real_fsm)
    assert dispatcher.storage is not storage
    await dispatcher.emit_shutdown()
    assert not closed
    await real_fsm.close()
    assert closed == [True]


async def test_terminal_stateless_handlers_overlap_without_early_postprocessing():
    async with application() as (dispatcher, bot, storage, isolation):
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        finish = asyncio.Event()
        finished = []

        async def post_action(handler, event, data):
            result = await handler(event, data)
            finished.append(event.message_id)
            return result

        async def handler(message):
            (first_entered if message.message_id == 1 else second_entered).set()
            await finish.wait()

        dispatcher.message.outer_middleware(post_action)
        dispatcher.message.register(handler, flags={"fsm_release": True})
        one = asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=message(1))))
        await first_entered.wait()
        two = asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=2, message=message(2))))
        await asyncio.wait_for(second_entered.wait(), 1)
        assert not finished
        finish.set()
        await asyncio.gather(one, two)
        assert sorted(finished) == [1, 2]
        assert isolation.key_count == 0


async def test_unflagged_handler_reads_state_after_previous_transition():
    async with application() as (dispatcher, bot, storage, isolation):
        first_entered = asyncio.Event()
        finish_first = asyncio.Event()
        seen = []

        async def handler(message, state, raw_state):
            seen.append(raw_state)
            if message.message_id == 1:
                first_entered.set()
                await finish_first.wait()
                await state.set_state("waiting")

        dispatcher.message.register(handler)
        one = asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=message(1))))
        await first_entered.wait()
        two = asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=2, message=message(2))))
        await asyncio.sleep(0)
        assert seen == [None]
        finish_first.set()
        await asyncio.gather(one, two)
        assert seen == [None, "waiting"]


async def test_inline_and_inaccessible_callbacks_never_resolve_shared_state():
    async with application() as (dispatcher, bot, storage, isolation):
        general = StorageKey(bot_id=bot.id, chat_id=KEY.chat_id, user_id=10)
        private = StorageKey(bot_id=bot.id, chat_id=10, user_id=10)
        await storage.set_state(general, "general-draft")
        await storage.set_state(private, "private-draft")
        seen = []

        async def callback_handler(query, state_context, state=None):
            seen.append((state_context.eligible, state))
            if state is not None:
                await state.clear()

        dispatcher.callback_query.register(callback_handler)
        user = User(id=10, is_bot=False, first_name="Synthetic")
        inaccessible = InaccessibleMessage(chat=message().chat, message_id=5, date=0)
        for index, payload in enumerate(({"message": inaccessible}, {"inline_message_id": "synthetic-inline"}), start=1):
            query = CallbackQuery(id=str(index), from_user=user, chat_instance="synthetic", data="cancel", **payload)
            await dispatcher.feed_update(bot, Update(update_id=index, callback_query=query))
        assert seen == [(False, None), (False, None)]
        assert await storage.get_state(general) == "general-draft"
        assert await storage.get_state(private) == "private-draft"
        assert isolation.key_count == 0


async def test_general_topics_and_users_have_independent_state_keys():
    async with application() as (dispatcher, bot, storage, isolation):
        keys = []

        async def handler(message, state):
            keys.append(state.key)

        dispatcher.message.register(handler)
        for index, (user, topic) in enumerate(((10, None), (10, 2), (10, 3), (11, 2)), start=1):
            await dispatcher.feed_update(bot, Update(update_id=index, message=message(index, user=user, topic=topic)))
        assert len(set(keys)) == 4
        assert [key.thread_id for key in keys] == [None, 2, 3, 2]


async def test_misflagged_skip_cannot_continue_with_unlocked_state():
    async with application() as (dispatcher, bot, storage, isolation):
        later = []

        async def skip(message):
            raise SkipHandler

        async def next_handler(message):
            later.append(True)

        dispatcher.message.register(skip, flags={"fsm_release": True})
        dispatcher.message.register(next_handler)
        with pytest.raises(RuntimeError, match="released terminal handler"):
            await dispatcher.feed_update(bot, Update(update_id=1, message=message()))
        assert not later
        assert isolation.key_count == 0


async def test_unmatched_viewer_boundary_can_release_before_conversion():
    async with application() as (dispatcher, bot, storage, isolation):
        entered = asyncio.Event()
        other_entered = asyncio.Event()
        finish = asyncio.Event()

        async def viewer(handler, event, data):
            result = await handler(event, data)
            assert result is UNHANDLED
            release_state_isolation(data["state_context"])
            (entered if event.message_id == 1 else other_entered).set()
            await finish.wait()
            return result

        dispatcher.message.outer_middleware(viewer)
        one = asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=message(1))))
        await entered.wait()
        two = asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=2, message=message(2))))
        await asyncio.wait_for(other_entered.wait(), 1)
        finish.set()
        await asyncio.gather(one, two)
        assert isolation.key_count == 0

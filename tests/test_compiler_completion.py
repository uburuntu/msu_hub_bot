"""Native compiler state, source editing and complete result delivery."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from teleforge.testing import RecordingBot

from msu_hub_bot.commands.control import process_cancel
from msu_hub_bot.commands.prog import ProgCompiler, ProgStates, register_code_submitters, register_code_submitters_with_stdin
from msu_hub_bot.telegram.filters import SlashCommand
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)


def message(bot, *, text=None, message_id=1, actor=7, topic=55, **kwargs):
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=-100123, type="supergroup"),
        from_user=User(id=actor, is_bot=actor == bot.id, first_name="Tester"),
        text=text,
        message_thread_id=topic,
        is_topic_message=topic is not None,
        **kwargs,
    ).as_(bot)


@asynccontextmanager
async def compiler_dispatcher(provider, storage=None):
    dispatcher = Dispatcher(disable_fsm=True, jdoodle=provider)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(storage or MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    for observer in (dispatcher.message, dispatcher.edited_message, dispatcher.callback_query):
        observer.middleware(SelectiveIsolationMiddleware())
    router = Router()
    router.message.register(process_cancel, SlashCommand("cancel"), flags={"fsm_release": False})
    router.callback_query.register(ProgCompiler.process_stdin_cb, ProgCompiler.callback_data.filter(), flags={"fsm_release": False})
    router.message.register(ProgCompiler.process_stdin_run, StateFilter(ProgStates.stdin), flags={"fsm_release": False})
    register_code_submitters(router)
    register_code_submitters_with_stdin(router)
    dispatcher.include_router(router)
    try:
        yield dispatcher
    finally:
        await fsm.close()


@pytest.fixture(autouse=True)
def native_preview_cache():
    ProgCompiler.replies.clear()
    yield
    ProgCompiler.replies.clear()


@pytest.mark.parametrize(
    "incoming,stdin",
    [
        ({"text": "#cancel"}, "#cancel"),
        ({"text": "/cancel@otherbot"}, "/cancel@otherbot"),
        (
            {
                "caption": "/cancel",
                "photo": [{"file_id": "synthetic", "file_unique_id": "synthetic", "width": 16, "height": 16}],
            },
            "/cancel",
        ),
    ],
)
async def test_non_cancel_messages_remain_program_input(incoming, stdin):
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="done")))
    async with compiler_dispatcher(provider) as app:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(input())")))
        preview = bot.requests[-1]
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id)
        click = CallbackQuery(
            id="q",
            from_user=User(id=7, is_bot=False, first_name="Actor"),
            chat_instance="c",
            message=ui,
            data="prog:input",
        )
        await app.feed_update(bot, Update(update_id=2, callback_query=click))
        await app.feed_update(bot, Update(update_id=3, message=message(bot, **incoming)))
    provider.instance.request_and_parse.assert_awaited_once_with("print(input())", stdin, "python3")


async def test_large_source_document_uses_filename_preview_and_loads_on_click(monkeypatch):
    source = "print(input())\n" * 400
    download = AsyncMock(return_value=source)
    monkeypatch.setattr("msu_hub_bot.commands.prog.download_text", download)
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="done")))
    original = message(
        bot,
        caption="/py_stdin explanation",
        document={
            "file_id": "source",
            "file_unique_id": "source",
            "file_name": "code.py",
            "mime_type": "text/x-python",
            "file_size": len(source),
        },
    )
    async with compiler_dispatcher(provider) as app:
        await app.feed_update(bot, Update(update_id=1, message=original))
        download.assert_not_awaited()
        preview = bot.requests[-1]
        assert isinstance(preview, SendMessage)
        assert preview.text.endswith("code.py") and len(preview.text) < 100
        assert preview.reply_parameters.message_id == original.message_id
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id, reply_to_message=original)
        click = CallbackQuery(id="q", from_user=original.from_user, chat_instance="c", message=ui, data="prog:input")
        await app.feed_update(bot, Update(update_id=2, callback_query=click))
        download.assert_awaited_once_with("source", bot)
        await app.feed_update(bot, Update(update_id=3, message=message(bot, text="hello", message_id=2)))
    provider.instance.request_and_parse.assert_awaited_once_with(source, "hello", "python3")


async def test_editing_source_refreshes_the_same_preview_and_runs_revised_code():
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="done")))
    async with compiler_dispatcher(provider) as app:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(1)")))
        assert isinstance(bot.requests[-1], SendMessage)
        sent_id = bot.recording.next_message_id
        await app.feed_update(bot, Update(update_id=2, edited_message=message(bot, text="#py_stdin print(2)")))
        preview = bot.requests[-1]
        assert isinstance(preview, EditMessageText) and preview.message_id == sent_id
        assert "print(2)" in preview.text and "print(1)" not in preview.text
        ui = message(bot, message_id=sent_id, text=preview.text, entities=preview.entities, actor=bot.id)
        click = CallbackQuery(id="q", from_user=message(bot).from_user, chat_instance="c", message=ui, data="prog:input")
        await app.feed_update(bot, Update(update_id=3, callback_query=click))
        await app.feed_update(bot, Update(update_id=4, message=message(bot, text="hello", message_id=2)))
    provider.instance.request_and_parse.assert_awaited_once_with("print(2)", "hello", "python3")


async def test_program_execution_releases_the_finished_conversation():
    started, finish = asyncio.Event(), asyncio.Event()

    async def run(*args):
        started.set()
        await finish.wait()
        return "done"

    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(side_effect=run)))
    async with compiler_dispatcher(provider) as app:

        async def ping(message: Message):
            return await message.reply("pong")

        app.message.register(ping, Command("ping"), StateFilter(None))
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(input())")))
        preview = bot.requests[-1]
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id)
        click = CallbackQuery(id="q", from_user=message(bot).from_user, chat_instance="c", message=ui, data="prog:input")
        await app.feed_update(bot, Update(update_id=2, callback_query=click))
        execution = asyncio.create_task(app.feed_update(bot, Update(update_id=3, message=message(bot, text="hello", message_id=2))))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await asyncio.wait_for(app.feed_update(bot, Update(update_id=4, message=message(bot, text="/ping", message_id=3))), timeout=2)
            assert bot.requests[-1].text == "pong"
            assert not execution.done()
        finally:
            finish.set()
            await execution


@pytest.mark.parametrize("already_deleted", [False, True])
async def test_callback_cancel_deletes_its_saved_prompt_only_in_the_owning_scope(already_deleted):
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="done")))

    async def respond(bot, method):
        if already_deleted and isinstance(method, DeleteMessage):
            return TelegramBadRequest(method=method, message="Bad Request: message to delete not found")
        return bot.recording._default(bot, method)

    bot.recording.responder = respond
    async with compiler_dispatcher(provider) as app:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(input())")))
        preview = bot.requests[-1]
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id)

        async def click(action, *, actor=7, topic=55):
            query = CallbackQuery(
                id=f"{action}-{actor}-{topic}",
                from_user=User(id=actor, is_bot=False, first_name="Actor"),
                chat_instance="c",
                message=ui.model_copy(update={"message_thread_id": topic}),
                data=f"prog:{action}",
            )
            await app.feed_update(bot, Update(update_id=2, callback_query=query))

        await click("input")
        waiting_id = bot.recording.next_message_id
        assert "ожидаю ввод ⬇️, или /cancel" in bot.requests[-1].text
        for actor, topic in ((9, 55), (7, 56)):
            start = len(bot.requests)
            await click("cancel", actor=actor, topic=topic)
            assert len(bot.requests[start:]) == 1
            assert isinstance(bot.requests[-1], AnswerCallbackQuery)
            assert bot.requests[-1].text == "💁🏻‍♂️ Вы не в процессе ввода"
        start = len(bot.requests)
        await click("cancel")
        deleted, answered = bot.requests[start:]
        assert isinstance(deleted, DeleteMessage)
        assert (deleted.chat_id, deleted.message_id) == (ui.chat.id, waiting_id)
        assert isinstance(answered, AnswerCallbackQuery)
        assert answered.text == "🆗 Ввод отменён"
        await app.feed_update(bot, Update(update_id=3, message=message(bot, text="late input", message_id=2)))
    provider.instance.request_and_parse.assert_not_awaited()


@pytest.mark.parametrize("output", ["<literal output>", "🦊" * 3000, ""])
async def test_native_compiler_completion_preserves_full_output_and_original_source(output):
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value=output)))
    source = message(bot, text="print(1)", message_id=9)
    async with compiler_dispatcher(provider) as dispatcher:
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(bot, text="/py", reply_to_message=source)))
    provider.instance.request_and_parse.assert_awaited_once_with("print(1)", "", "python3")
    files = [item for item in bot.requests if isinstance(item, SendDocument)]
    if len(output) > 2000:
        assert len(files) == 1 and files[0].document.filename == "result.txt"
        assert files[0].document.data.decode().endswith(output)
        assert files[0].reply_parameters.message_id == 9 and files[0].message_thread_id == 55
        assert bot.requests[-1].text == "Готово — полный результат в файле."
    else:
        assert not files
        assert bot.requests[-1].text.endswith(output) if output else "ошибка" in bot.requests[-1].text
        assert bot.requests[-1].parse_mode is None
        assert bot.requests[-1].entities[-1].type == ("pre" if output else "code")


async def test_native_compiler_reuses_cached_status_for_edited_source():
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(side_effect=["first", "second"])))
    async with compiler_dispatcher(provider) as dispatcher:
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(bot, text="#py print(1)")))
        status_id = bot.requests[-1].message_id
        await dispatcher.feed_update(bot, Update(update_id=2, edited_message=message(bot, text="#py print(2)")))
    sends = [item for item in bot.requests if isinstance(item, SendMessage)]
    assert len(sends) == 1 and bot.requests[-1].message_id == status_id
    assert bot.requests[-1].text.endswith("second")
    assert provider.instance.request_and_parse.await_count == 2


async def test_native_draft_survives_dispatcher_replacement_and_isolates_actor_topic():
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="done")))
    storage = MemoryStorage()
    async with compiler_dispatcher(provider, storage) as dispatcher:
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(input())")))
        preview = bot.requests[-1]
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id)
        click = CallbackQuery(id="q", from_user=message(bot).from_user, chat_instance="c", message=ui, data="prog:input")
        await dispatcher.feed_update(bot, Update(update_id=2, callback_query=click))
    async with compiler_dispatcher(provider, storage) as replacement:
        for actor, topic in ((8, 55), (7, 56)):
            await replacement.feed_update(bot, Update(update_id=3, message=message(bot, text="wrong", actor=actor, topic=topic)))
        provider.instance.request_and_parse.assert_not_awaited()
        await replacement.feed_update(bot, Update(update_id=4, message=message(bot, text="right")))
        await replacement.feed_update(bot, Update(update_id=5, message=message(bot, text="too late")))
    provider.instance.request_and_parse.assert_awaited_once_with("print(input())", "right", "python3")


async def test_native_slash_cancel_prevents_later_provider_execution():
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock()))
    async with compiler_dispatcher(provider) as dispatcher:
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(input())")))
        preview = bot.requests[-1]
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id)
        click = CallbackQuery(id="q", from_user=message(bot).from_user, chat_instance="c", message=ui, data="prog:input")
        await dispatcher.feed_update(bot, Update(update_id=2, callback_query=click))
        await dispatcher.feed_update(bot, Update(update_id=3, message=message(bot, text="/cancel")))
        assert bot.requests[-1].text == "👌🏻"
        await dispatcher.feed_update(bot, Update(update_id=4, message=message(bot, text="later")))
    provider.instance.request_and_parse.assert_not_awaited()


async def test_native_compiler_cancellation_updates_status_and_propagates():
    bot = RecordingBot()
    started = asyncio.Event()

    async def run(*args):
        started.set()
        await asyncio.Event().wait()

    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=run))
    async with compiler_dispatcher(provider) as dispatcher:
        task = asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=message(bot, text="#py print(1)"))))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert bot.requests[-1].text == "Выполнение отменено."
    assert not any(isinstance(item, SendDocument) for item in bot.requests)


@pytest.mark.parametrize("cancel", [False, True])
async def test_applied_but_unconfirmed_compiler_edit_is_never_overwritten(cancel):
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="actual result")))
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
    async with compiler_dispatcher(provider) as dispatcher:
        task = asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=message(bot, text="#py print(1)"))))
        await asyncio.wait_for(applied.wait(), 2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
            assert isinstance(bot.requests[-1], SendMessage) and bot.requests[-1].reply_parameters.message_id == 1
    assert list(visible.values())[0].endswith("actual result")
    assert len([item for item in bot.requests if isinstance(item, EditMessageText)]) == 1
    provider.instance.request_and_parse.assert_awaited_once()


@pytest.mark.parametrize("stdin", [False, True])
async def test_native_compiler_waiting_preview_prompt_and_result_keep_direct_message_topic(stdin):
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="x" * 5000)))

    async def respond(bot, method):
        if isinstance(method, (SendMessage, SendDocument)) and method.direct_messages_topic_id != 77:
            return TelegramBadRequest(method=method, message="Bad Request: Channel direct messages topic must be specified")
        return bot.recording._default(bot, method)

    bot.recording.responder = respond
    original = message(
        bot, text="#py_stdin print(input())" if stdin else "#py print(1)", topic=None, direct_messages_topic={"topic_id": 77}
    )
    async with compiler_dispatcher(provider) as dispatcher:
        await dispatcher.feed_update(bot, Update(update_id=1, message=original))
        if stdin:
            preview = bot.requests[-1]
            ui = message(
                bot,
                message_id=101,
                text=preview.text,
                entities=preview.entities,
                actor=bot.id,
                topic=None,
                direct_messages_topic={"topic_id": 77},
            )
            click = CallbackQuery(id="q", from_user=original.from_user, chat_instance="c", message=ui, data="prog:input")
            await dispatcher.feed_update(bot, Update(update_id=2, callback_query=click))
            await dispatcher.feed_update(
                bot, Update(update_id=3, message=message(bot, text="input", topic=None, direct_messages_topic={"topic_id": 77}))
            )
    sends = [item for item in bot.requests if isinstance(item, (SendMessage, SendDocument))]
    assert len(sends) == (4 if stdin else 2)
    assert all(item.direct_messages_topic_id == 77 for item in sends)
    assert isinstance(sends[-1], SendDocument)
    provider.instance.request_and_parse.assert_awaited_once()


async def test_native_cancel_in_direct_message_topic_acknowledges_and_clears_stdin():
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock()))
    original = message(bot, text="#py_stdin print(input())", topic=None, direct_messages_topic={"topic_id": 77})
    async with compiler_dispatcher(provider) as dispatcher:
        await dispatcher.feed_update(bot, Update(update_id=1, message=original))
        preview = bot.requests[-1]
        ui = message(
            bot,
            message_id=101,
            text=preview.text,
            entities=preview.entities,
            actor=bot.id,
            topic=None,
            direct_messages_topic={"topic_id": 77},
        )
        click = CallbackQuery(id="q", from_user=original.from_user, chat_instance="c", message=ui, data="prog:input")
        await dispatcher.feed_update(bot, Update(update_id=2, callback_query=click))
        await dispatcher.feed_update(
            bot, Update(update_id=3, message=message(bot, text="/cancel", topic=None, direct_messages_topic={"topic_id": 77}))
        )
        assert bot.requests[-1].text == "👌🏻" and bot.requests[-1].direct_messages_topic_id == 77
        await dispatcher.feed_update(
            bot, Update(update_id=4, message=message(bot, text="too late", topic=None, direct_messages_topic={"topic_id": 77}))
        )
    provider.instance.request_and_parse.assert_not_awaited()

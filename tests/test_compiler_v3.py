"""Compiler alias, callback and consumed-input contracts using real v3 dispatch."""

import asyncio
from types import SimpleNamespace

import pytest
from aiogram import Dispatcher, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from common.tg.state import IsolationScope, UpdateStateContext
from hub_bot.commands.prog import (
    ProgCallback,
    ProgCompiler,
    ProgStates,
    StdinDraft,
    register_code_submitters,
    register_code_submitters_with_stdin,
)
from hub_bot.utils.jdoodle import JDoodle, JDoodleResponse
from telegram_helpers import make_bot, make_message


@pytest.mark.parametrize("event", ["message", "edited_message"])
@pytest.mark.parametrize(
    "text,mode",
    [
        ("#py_stdin print(1)", "stdin"),
        ("#python_stdin print(1)", "stdin"),
        ("#PY_STDIN print(1)", "stdin"),
        ("/py_stdin print(1)", "stdin"),
        ("#pys print(1)", "stdin"),
        ("#py #pys print(1)", "ordinary"),
        ("#pys #py print(1)", "ordinary"),
        ("#python3_stdin print(1)", "ordinary"),
    ],
)
@pytest.mark.asyncio
async def test_actual_dispatch_changes_only_the_approved_stdin_precedence(monkeypatch, event, text, mode):
    def builder(mode):
        def for_language(lang):
            async def handle(message, meta):
                return mode, lang, meta.text

            return handle

        return for_language

    monkeypatch.setattr(ProgCompiler, "process_builder", builder("ordinary"))
    monkeypatch.setattr(ProgCompiler, "process_stdin_builder", builder("stdin"))
    router = Router()
    register_code_submitters(router)
    register_code_submitters_with_stdin(router)
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(router)
    bot = make_bot()
    update = Update(update_id=1, **{event: make_message(bot, text=text)})
    result = await dispatcher.feed_update(bot, update)
    assert result[:2] == (mode, "python3")
    assert len(router.message.handlers) == len(router.edited_message.handlers) == 147
    await dispatcher.fsm.close()


@pytest.mark.asyncio
async def test_consumed_stdin_releases_isolation_and_preserves_a_later_draft():
    bot = make_bot()
    message = make_message(bot, text="stdin", is_topic_message=True, message_thread_id=9)
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=bot.id, chat_id=message.chat.id, user_id=42, thread_id=9))
    draft = StdinDraft(chat_id=message.chat.id, inform_message_id=3, prog_lang="python3", prog_code="print(input())")
    await state.set_state(ProgStates.stdin)
    await state.set_data(draft.model_dump())
    started, release = asyncio.Event(), asyncio.Event()
    lock = asyncio.Lock()

    async def compile_code(code, stdin, lang):
        assert (code, stdin, lang) == ("print(input())", "stdin", "python3")
        assert await state.get_state() is None
        assert not lock.locked()
        started.set()
        await release.wait()
        return "result"

    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=compile_code))

    async def consume():
        await lock.acquire()
        context = UpdateStateContext(eligible=True)
        context.scope = IsolationScope(lock, asyncio.current_task())
        return await ProgCompiler.process_stdin_run(message, state, bot, provider, context)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    await state.clear()  # /cancel after consumption must not cancel the external operation.
    await state.set_state("StickerStates:sticker_set_name")
    await state.set_data({"title": "new draft"})
    release.set()
    await task
    assert await state.get_state() == "StickerStates:sticker_set_name"
    assert await state.get_data() == {"title": "new draft"}
    assert bot.session.methods[-1].text.endswith("<pre>result</pre>")
    await storage.close()


def test_callback_wire_values_and_rows_remain_compatible():
    assert ProgCallback.unpack("prog:input").action == "input"
    assert ProgCallback(action="cancel").pack() == "prog:cancel"
    assert [button.callback_data for button in ProgCompiler.keyboard().inline_keyboard[0]] == ["prog:input", "prog:cancel"]


def test_jdoodle_models_keep_missing_optional_and_numeric_string_fields():
    response = JDoodleResponse.model_validate({"statusCode": 200, "memory": 0, "cpuTime": 1.2})
    assert response.output is None
    assert (response.memory, response.cpuTime) == ("0", "1.2")


@pytest.mark.asyncio
async def test_closing_unused_compiler_client_does_not_open_a_session():
    client = JDoodle("synthetic", "synthetic")
    await client.close()
    assert "session" not in client.__dict__


def test_compiler_route_keys_are_stable_across_aliases_and_events():
    router = Router()
    register_code_submitters(router)
    register_code_submitters_with_stdin(router)
    for observer in (router.message, router.edited_message):
        keys = [handler.flags["handler_key"] for handler in observer.handlers]
        assert keys.count("compile.python3") == 2
        assert keys.count("compile.stdin_prompt.python3") == 3
        assert keys[0] == "compile.stdin_prompt.python3"
        assert all(key.startswith("compile.") for key in keys)

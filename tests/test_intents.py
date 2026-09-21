"""Explicit mentions select bounded reply commands without changing their inputs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import BaseMiddleware, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Document, Message, MessageEntity, Update, User

from msu_hub_bot.commands import intents
from msu_hub_bot.commands.intents import IntentCommands
from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES
from msu_hub_bot.providers.jev import JevDecision, JevError, JevErrorReason, ReplyMetadata
from msu_hub_bot.providers.wit import Wit
from msu_hub_bot.providers.wolfram import WolframAPI
from msu_hub_bot.routing import build_router
from msu_hub_bot.settings import MissingIntegration, Settings
from msu_hub_bot.telegram.command_api import DocumentInput, MetaCommand, MetaInfo
from msu_hub_bot.telegram.extraction import Extractor, SimpleExtractor
from msu_hub_bot.telegram.mentions import IntentMention
from msu_hub_bot.telegram.middlewares.settings import Settings as ChatSettings
from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware, preview_policy
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)
from msu_hub_bot.telemetry import Telemetry
from telegram_helpers import RecordingSession, make_bot, make_message


PHOTO = {"file_id": "photo", "file_unique_id": "photo", "width": 32, "height": 24, "file_size": 10}
DOCUMENT = {"file_id": "doc", "file_unique_id": "doc", "file_name": "source.docx", "mime_type": "application/msword", "file_size": 20}
VOICE = {"file_id": "voice", "file_unique_id": "voice", "duration": 1, "file_size": 10}
VIDEO = {"file_id": "video", "file_unique_id": "video", "duration": 1, "width": 32, "height": 24, "file_size": 10}


def entity(text, token="@test_bot", **fields):
    start = text.index(token)
    return MessageEntity(
        type="mention",
        offset=len(text[:start].encode("utf-16-le")) // 2,
        length=len(token.encode("utf-16-le")) // 2,
        **fields,
    )


def source(bot, **fields):
    return make_message(
        bot,
        message_id=70,
        message_thread_id=17,
        is_topic_message=True,
        from_user={"id": 99, "is_bot": False, "first_name": "Source author"},
        **fields,
    )


def invocation(bot, reply=None, text="@test_bot сделай", **fields):
    return make_message(
        bot,
        message_id=80,
        message_thread_id=17,
        is_topic_message=True,
        reply_to_message=reply,
        text=text,
        entities=[entity(text)] if "@test_bot" in text else [],
        **fields,
    )


@pytest.fixture
async def runtime():
    bot = make_bot()
    bot._me = User(id=bot.id, is_bot=True, first_name="Synthetic", username="test_bot")
    client = SimpleNamespace(classify=AsyncMock(return_value=JevDecision(command="bg", confidence=0.9)))
    clock = [0.0]
    service = IntentCommands(client, telemetry=Telemetry(), clock=lambda: clock[0])
    try:
        yield SimpleNamespace(bot=bot, client=client, service=service, clock=clock, executor=object())
    finally:
        await bot.session.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@test_bot, убери фон", "убери фон"),
        ("🧩 убери фон, @test_bot!", "🧩 убери фон, !"),
        ("@test_bot убери фон 🧩 @TEST_BOT", "убери фон 🧩"),
    ],
)
async def test_mentions_remove_only_matching_utf16_entities(runtime, text, expected):
    mentions = [entity(text)]
    if "@TEST_BOT" in text:
        mentions.append(entity(text, "@TEST_BOT"))
    message = make_message(runtime.bot, text=text, entities=mentions)
    assert await IntentMention()(message, runtime.bot) == {"intent_request": expected}
    assert runtime.bot.session.methods == []


async def test_text_mention_uses_user_identity_not_display_name(runtime):
    text = "🧩 Наш бот — сделай PDF"
    mention = MessageEntity(type="text_mention", offset=3, length=7, user=runtime.bot._me)
    message = make_message(runtime.bot, text=text, entities=[mention])
    assert await IntentMention()(message, runtime.bot) == {"intent_request": "🧩  — сделай PDF"}
    wrong = mention.model_copy(update={"user": User(id=456, is_bot=True, first_name="test_bot")})
    assert await IntentMention()(message.model_copy(update={"entities": [wrong]}), runtime.bot) is False


@pytest.mark.parametrize(
    "case",
    ["no_entity", "wrong_bot", "reply_only", "caption", "rich", "slash", "hashtag", "bot_actor", "disabled"],
)
async def test_ineligible_mentions_do_not_enter_intent_route(runtime, case):
    bot = runtime.bot
    message = invocation(bot)
    if case == "no_entity":
        message = message.model_copy(update={"entities": []})
    elif case == "wrong_bot":
        message = make_message(bot, text="@other_bot сделай", entities=[entity("@other_bot", "@other_bot")])
    elif case == "reply_only":
        message = invocation(bot, invocation(bot), text="убери фон")
    elif case == "caption":
        message = make_message(bot, photo=[PHOTO], caption="@test_bot сделай", caption_entities=[entity("@test_bot")])
    elif case == "rich":
        message = make_message(bot, rich_message={"blocks": [{"type": "paragraph", "text": "@test_bot сделай"}]})
    elif case in {"slash", "hashtag"}:
        message = invocation(bot, text=("/roll " if case == "slash" else "#py ") + "@test_bot")
    elif case == "bot_actor":
        message = message.model_copy(update={"from_user": bot._me})
    assert await IntentMention(enabled=case != "disabled")(message, bot) is False
    assert bot.session.methods == []


@pytest.mark.parametrize(
    ("command", "attachment"),
    [
        ("pdf", {"document": DOCUMENT}),
        ("text", {"photo": [PHOTO]}),
        ("bg", {"photo": [PHOTO]}),
        ("song", {"voice": VOICE}),
        ("song", {"video": VIDEO}),
        ("anime", {"photo": [PHOTO]}),
    ],
)
async def test_selected_adapter_keeps_source_caller_topic_and_empty_command_payload(runtime, monkeypatch, command, attachment):
    bot = runtime.bot
    original = source(bot, caption="PRIVATE_SOURCE_TEXT", **attachment)
    message = invocation(bot, original)
    runtime.client.classify.return_value = JevDecision(command=command, confidence=0.8)
    profile = AsyncMock(side_effect=AssertionError("Intent commands must never select an avatar"))
    monkeypatch.setattr(SimpleExtractor, "profile_photo", profile)

    async def execute(*args):
        assert args[0] is message and args[0].from_user.id == 42
        if command in {"pdf", "text"}:
            meta = args[1]
            assert meta.command == command and meta.text == "" and meta.arguments == []
            assert meta.reply_target() is original
            selected, media = await (meta.extract_doc() if command == "pdf" else meta.extract_image())
            if command == "text":
                assert args[2] is runtime.executor
        elif command in {"bg", "anime"}:
            selected, media = await Extractor.image(message, with_profile_photo=True)
            if command == "bg":
                assert args[1] is runtime.executor
        else:
            assert args[1:] == (bot, runtime.executor)
            selected = message.reply_to_message
            media = selected.voice or selected.video
        assert selected is original and media.file_id in {"doc", "photo", "voice", "video"}
        return await selected.reply("Processed")

    adapter = AsyncMock(side_effect=execute)
    if command == "pdf":

        @MetaCommand("pdf", document=DocumentInput(reply=True))
        async def pdf_adapter(document: Document, meta: MetaInfo) -> Message:
            assert document is original.document
            return await adapter(meta.message, meta)

        monkeypatch.setattr(intents, "process_topdf", pdf_adapter)
    else:
        monkeypatch.setattr(intents, intents.COMMANDS[command][0], adapter)
    await runtime.service.handle(message, "REQUEST_ONLY", bot, runtime.executor)
    adapter.assert_awaited_once()
    profile.assert_not_awaited()
    request, metadata = runtime.client.classify.await_args.args
    assert request == "REQUEST_ONLY" and isinstance(metadata, ReplyMetadata)
    assert "PRIVATE_SOURCE_TEXT" not in metadata.model_dump_json()
    assert "source.docx" not in metadata.model_dump_json()
    result = next(method for method in bot.session.methods if method.__api_method__ == "sendMessage")
    assert result.chat_id == original.chat.id and result.message_thread_id == 17
    assert result.reply_parameters.message_id == original.message_id
    assert runtime.service._active == set()


@pytest.mark.parametrize("mime_type", ["-foo/bar", ".foo/bar", "foo/-bar", "foo/_bar"])
async def test_invalid_document_mime_is_omitted_without_blocking_pdf(runtime, monkeypatch, mime_type):
    original = source(runtime.bot, document={**DOCUMENT, "mime_type": mime_type})
    message = invocation(runtime.bot, original)
    runtime.client.classify.return_value = JevDecision(command="pdf", confidence=0.9)
    adapter = AsyncMock(return_value=original)

    @MetaCommand("pdf", document=DocumentInput(reply=True))
    async def pdf_adapter(document: Document, meta: MetaInfo) -> Message:
        assert document is original.document
        return await adapter(meta.message, meta)

    monkeypatch.setattr(intents, "process_topdf", pdf_adapter)

    assert await runtime.service.handle(message, "сделай PDF", runtime.bot, runtime.executor) is original

    runtime.client.classify.assert_awaited_once_with("сделай PDF", ReplyMetadata(document=True))
    adapter.assert_awaited_once()
    called_message, meta = adapter.await_args.args
    assert called_message is message
    selected, document = await meta.extract_doc()
    assert selected is original and document is original.document
    assert document.mime_type == mime_type


@pytest.mark.parametrize("case", ["no_reply", "empty", "too_long", "text_only", "nested", "wrong_chat", "wrong_topic", "own_attachment"])
async def test_invalid_input_never_classifies_or_uses_profile_fallback(runtime, monkeypatch, case):
    original = source(runtime.bot, photo=[PHOTO])
    message = invocation(runtime.bot, original)
    request = "убери фон"
    if case == "no_reply":
        message = invocation(runtime.bot)
    elif case in {"empty", "too_long"}:
        request = "" if case == "empty" else "x" * 1501
    elif case == "text_only":
        message = invocation(runtime.bot, source(runtime.bot, text="No attachment"))
    elif case == "nested":
        message = invocation(runtime.bot, source(runtime.bot, text="Reply only", reply_to_message=original))
    elif case == "wrong_chat":
        message = invocation(runtime.bot, original.model_copy(update={"chat": original.chat.model_copy(update={"id": -100999})}))
    elif case == "wrong_topic":
        message = invocation(runtime.bot, original.model_copy(update={"message_thread_id": 18}))
    else:
        message = invocation(runtime.bot, original, photo=[{**PHOTO, "file_id": "own-photo"}])
    profile = AsyncMock(side_effect=AssertionError("No avatar fallback"))
    execute = AsyncMock()
    monkeypatch.setattr(SimpleExtractor, "profile_photo", profile)
    monkeypatch.setattr(runtime.service, "_execute", execute)
    await runtime.service.handle(message, request, runtime.bot, runtime.executor)
    runtime.client.classify.assert_not_awaited()
    execute.assert_not_awaited()
    profile.assert_not_awaited()
    assert runtime.service._active == set() and not runtime.service._recent


@pytest.mark.parametrize("case", ["none", "low_confidence", "wrong_media", "too_large", "timeout", "provider_error"])
async def test_declines_and_provider_errors_do_not_execute(runtime, monkeypatch, case):
    original = source(runtime.bot, photo=[{**PHOTO, "file_size": MAX_DOWNLOAD_BYTES + 1 if case == "too_large" else 10}])
    execute = AsyncMock()
    monkeypatch.setattr(runtime.service, "_execute", execute)
    runtime.client.classify.return_value = JevDecision(
        command="none" if case == "none" else "pdf" if case == "wrong_media" else "bg",
        confidence=0.799 if case == "low_confidence" else 0.9,
    )
    if case in {"timeout", "provider_error"}:
        runtime.client.classify.side_effect = JevError(JevErrorReason.TIMEOUT if case == "timeout" else JevErrorReason.INVALID_RESPONSE)
    await runtime.service.handle(invocation(runtime.bot, original), "сделай", runtime.bot, runtime.executor)
    runtime.client.classify.assert_awaited_once()
    execute.assert_not_awaited()
    assert any(method.__api_method__ == "sendMessage" for method in runtime.bot.session.methods)
    assert runtime.service._active == set()


@pytest.mark.parametrize("failure", [True, MissingIntegration("synthetic_key")])
async def test_existing_adapter_decline_returns_actionable_reply(runtime, monkeypatch, failure):
    adapter = AsyncMock(return_value=failure, side_effect=failure if isinstance(failure, Exception) else None)
    monkeypatch.setattr(intents, "process_bg", adapter)
    await runtime.service.handle(invocation(runtime.bot, source(runtime.bot, photo=[PHOTO])), "убери фон", runtime.bot, runtime.executor)
    reply = next(method for method in runtime.bot.session.methods if method.__api_method__ == "sendMessage")
    assert "Попробуй" in reply.text and "synthetic_key" not in reply.text
    assert runtime.service._active == set()


async def test_cooldown_is_per_caller_and_expires(runtime, monkeypatch):
    monkeypatch.setattr(intents, "process_bg", AsyncMock(return_value=make_message(runtime.bot)))
    message = invocation(runtime.bot, source(runtime.bot, photo=[PHOTO]))
    await runtime.service.handle(message, "убери фон", runtime.bot, runtime.executor)
    await runtime.service.handle(message.model_copy(update={"message_id": 81}), "убери фон", runtime.bot, runtime.executor)
    assert runtime.client.classify.await_count == 1
    runtime.clock[0] = 5.1
    await runtime.service.handle(message, "убери фон", runtime.bot, runtime.executor)
    assert runtime.client.classify.await_count == 2


async def test_concurrency_limit_and_cancellation_release_capacity(runtime, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    arrivals = 0

    async def classify(*args):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 4:
            entered.set()
        await release.wait()
        return JevDecision(command="bg", confidence=0.9)

    runtime.client.classify.side_effect = classify
    monkeypatch.setattr(intents, "process_bg", AsyncMock(return_value=make_message(runtime.bot)))
    original = source(runtime.bot, photo=[PHOTO])
    messages = [
        invocation(runtime.bot, original, from_user={"id": actor, "is_bot": False, "first_name": "Caller"}) for actor in range(1, 6)
    ]
    tasks = [asyncio.create_task(runtime.service.handle(message, "сделай", runtime.bot, runtime.executor)) for message in messages[:4]]
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await runtime.service.handle(messages[0], "ещё", runtime.bot, runtime.executor)
        await runtime.service.handle(messages[4], "ещё", runtime.bot, runtime.executor)
        assert runtime.client.classify.await_count == 4 and runtime.service._active == {1, 2, 3, 4}
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert runtime.service._active == {2, 3, 4}
        release.set()
        await runtime.service.handle(messages[4], "сделай", runtime.bot, runtime.executor)
        await asyncio.gather(*tasks[1:])
        assert runtime.client.classify.await_count == 5 and runtime.service._active == set()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "chat-action" and not task.done()]


class IntentSelection(BaseMiddleware):
    async def __call__(self, handler, event, data):
        flags = data["handler"].flags
        if flags["handler_key"] == "process_intent":
            assert flags["automatic_previews"] is False and flags["fsm_release"] is True
            return await handler(event, data)
        return flags["handler_key"]


@pytest.fixture
async def dispatch(runtime):
    dispatcher = Dispatcher(disable_fsm=True, intents=runtime.service, cpu_executor=runtime.executor, settings=ChatSettings())
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    viewer = ViewerMiddleware(runtime.bot, Mock(), Mock())
    viewer.view = AsyncMock()
    dispatcher.message.outer_middleware(viewer)
    for observer in (dispatcher.message, dispatcher.edited_message):
        observer.middleware(SelectiveIsolationMiddleware())
        observer.middleware(preview_policy)
        observer.middleware(IntentSelection())
    dispatcher.include_router(build_router(wit=Wit([]), wolfram=WolframAPI(""), config=Settings(jev_enabled=True)))
    try:
        yield dispatcher, fsm, viewer
    finally:
        await fsm.close()


async def test_real_dispatch_executes_only_explicit_new_mention_and_suppresses_previews(runtime, dispatch, monkeypatch):
    dispatcher, _, viewer = dispatch
    adapter = AsyncMock(return_value=make_message(runtime.bot))
    monkeypatch.setattr(intents, "process_bg", adapter)
    message = invocation(runtime.bot, source(runtime.bot, photo=[PHOTO]), text="@test_bot убери фон https://x.com/test/status/123")
    await dispatcher.feed_update(runtime.bot, Update(update_id=1, message=message))
    adapter.assert_awaited_once()
    runtime.client.classify.assert_awaited_once()
    viewer.view.assert_not_awaited()
    assert runtime.client.classify.await_args.args[0] == "убери фон https://x.com/test/status/123"


@pytest.mark.parametrize(
    ("text", "state", "edited", "expected"),
    [
        ("/roll @test_bot", None, False, "process_roll"),
        ("@test_bot убери фон", "ProgStates:stdin", False, "ProgCompiler.process_stdin_run"),
        ("/cancel @test_bot", "ProgStates:stdin", False, "process_cancel"),
        ("@test_bot убери фон", None, True, UNHANDLED),
    ],
)
async def test_explicit_commands_fsm_and_edits_keep_ownership(runtime, dispatch, text, state, edited, expected):
    dispatcher, fsm, _ = dispatch
    message = invocation(runtime.bot, source(runtime.bot, photo=[PHOTO]), text=text)
    context = fsm.resolve_context(runtime.bot, chat_id=message.chat.id, user_id=42, thread_id=17)
    if state:
        await context.set_state(state)
    result = await dispatcher.feed_update(runtime.bot, Update(update_id=1, **{"edited_message" if edited else "message": message}))
    assert result == expected
    runtime.client.classify.assert_not_awaited()
    assert await context.get_state() == state


async def test_disabled_application_does_not_allocate_jev_client(monkeypatch):
    from msu_hub_bot import app

    session, database = RecordingSession(), AsyncMock()
    database.feature_request.return_value = None
    database.list_directory.return_value = []
    database.load_settings.return_value = {}
    client = Mock(side_effect=AssertionError("Disabled integration must not allocate a client"))
    monkeypatch.setattr(app, "JevClient", client)
    monkeypatch.setattr(app, "AiohttpSession", lambda **kwargs: session)
    monkeypatch.setattr(app, "create_repository", lambda *args, **kwargs: database)
    application = await app.Application.create(
        Settings(
            bot_token="123456789:" + "a" * 35,
            supabase_url="http://supabase.invalid",
            supabase_key="synthetic-publishable-key",
            supabase_email="bot@example.invalid",
            supabase_password="synthetic-password",
        )
    )
    try:
        assert application.dispatcher.workflow_data["intents"] is None
        message = invocation(application.bot, source(application.bot, photo=[PHOTO]))
        result = await asyncio.create_task(application.dispatcher.feed_update(application.bot, Update(update_id=1, message=message)))
        assert result is UNHANDLED
        client.assert_not_called()
    finally:
        await application.close()

"""Optional real-Derp dispatcher tests; run in an isolated PR29 environment."""

import sys
from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import derp
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerInlineQuery, EditMessageText
from aiogram.types import ChosenInlineResult, InlineQuery, Update, User
from aiogram.utils.i18n import I18n
from derp.common.sender import MessageSender
from derp.db import DatabaseManager
from derp.execution import Rejected, RejectionReason, Succeeded
from derp.features.inline_chat import FREE_INLINE_CHAT_PLAN, MAX_INLINE_QUERY_CHARS, InlineChatFeatureService, InlineProviderExecution
from derp.features.types import TextOutput
from derp.handlers.inline import _inline_request_id
from derp.inference import (
    FREE_INFERENCE_PRIVACY_VERSION,
    FREE_INFERENCE_TOS_VERSION,
    InferenceAttempt,
    InferencePrivacyMode,
    InferenceRecorder,
)
from derp.inference_usage import InferenceUsageId
from derp.middlewares.api_resilient import ResilientRequestMiddleware
from derp.middlewares.route_dependencies import setup_route_dependencies
from derp.models import User as UserModel
from teleforge import App
from teleforge.testing import RecordingBot, RecordingSession

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.derp.inline import InlineAnswers, _request_id

ACTOR = User(id=700, is_bot=False, first_name="Synthetic")
USER_ID = UUID("52ee6f22-6411-41bb-b8e5-1af379ecf508")
NOW = datetime(2026, 9, 22, tzinfo=UTC)


@pytest.fixture(autouse=True)
def translation():
    i18n = I18n(path=Path(derp.__file__).parent / "locales", default_locale="en", domain="messages")
    with i18n.context():
        yield


class ReadStore:
    """Only native SQL model lookup is synthetic; middleware/session scope is real."""

    def __init__(self, *, consented=True, missing=False):
        self.user = (
            None
            if missing
            else UserModel(
                id=USER_ID,
                telegram_id=ACTOR.id,
                is_bot=False,
                first_name=ACTOR.first_name,
                inference_privacy_mode=(
                    InferencePrivacyMode.ALLOW_NON_ZDR_FREE.value if consented else InferencePrivacyMode.PRIVATE_ONLY.value
                ),
                inference_privacy_revision=2,
                free_inference_tos_version=FREE_INFERENCE_TOS_VERSION if consented else None,
                free_inference_privacy_version=FREE_INFERENCE_PRIVACY_VERSION if consented else None,
                free_inference_accepted_at=NOW if consented else None,
            )
        )
        self.active_sessions = self.reads = 0
        self.db = MagicMock(spec=DatabaseManager)
        self.db.read_session.side_effect = self.session

    @asynccontextmanager
    async def session(self):
        self.active_sessions += 1
        try:
            yield self
        finally:
            self.active_sessions -= 1

    async def execute(self, statement):
        assert statement.column_descriptions[0]["entity"] is UserModel
        assert statement.compile().params["telegram_id_1"] == ACTOR.id
        self.reads += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.user
        return result


def inline_service(store, *, output="**A useful answer**", failure=None):
    executor = AsyncMock()

    async def provide(plan, request, *, user_id):
        assert store.active_sessions == 0
        assert user_id == USER_ID and plan is FREE_INLINE_CHAT_PLAN
        if isinstance(failure, Exception):
            raise failure
        return InlineProviderExecution(failure if failure is not None else Succeeded(TextOutput(output)))

    executor.answer.side_effect = provide
    recorder = AsyncMock(spec=InferenceRecorder)
    recorder.start.return_value = InferenceAttempt(InferenceUsageId(uuid4()), FREE_INLINE_CHAT_PLAN.model)
    service = InlineChatFeatureService(executor, recorder, free_plan=FREE_INLINE_CHAT_PLAN)
    return service, executor, recorder


def host(store, *, output="**A useful answer**", failure=None):
    service, executor, recorder = inline_service(store, output=output, failure=failure)
    app, dispatcher = App(InlineAnswers(service)), Dispatcher()
    setup_route_dependencies(dispatcher, store.db)
    dispatcher.include_router(app.build_router())
    return app, dispatcher, executor, recorder


def chosen(*, query="A question", result_id=None, inline_id="inline-1", actor=ACTOR):
    return Update(
        update_id=2,
        chosen_inline_result=ChosenInlineResult(
            result_id=str(uuid4()) if result_id is None else result_id,
            from_user=actor,
            query=query,
            inline_message_id=inline_id,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "A question", " " * 3, "<b>unsafe & preview</b>", "x" * 300])
async def test_complete_offer_and_selection_uses_native_service_and_model_middleware(query):
    store, bot = ReadStore(), RecordingBot()
    app, dispatcher, executor, recorder = host(store)
    async with app:
        await dispatcher.feed_update(bot, Update(update_id=1, inline_query=InlineQuery(id="q", from_user=ACTOR, query=query, offset="")))
        offer = bot.requests[-1]
        assert isinstance(offer, AnswerInlineQuery)
        assert offer.cache_time == 300 and offer.is_personal
        assert bool(offer.button) is bool(query)
        assert len(offer.results) == 1
        article = offer.results[0]
        UUID(article.id)
        assert article.title == "Ask Derp"
        assert article.reply_markup.inline_keyboard[0][0].text == "Add Derp to your chat"
        expected = f"Ask Derp: {query[:200]}" if query else "Ask a question in this chat."
        assert article.description == expected
        assert store.reads == 0
        executor.answer.assert_not_awaited()
        await dispatcher.feed_update(bot, chosen(query=query, result_id=article.id))
    assert store.reads == 1 and store.active_sessions == 0
    edit = bot.requests[-1]
    assert isinstance(edit, EditMessageText)
    assert edit.inline_message_id == "inline-1" and edit.chat_id is None and edit.message_id is None
    assert edit.parse_mode == "HTML"
    if query.strip() and len(query) <= MAX_INLINE_QUERY_CHARS:
        executor.answer.assert_awaited_once()
        recorder.succeed_reports.assert_awaited_once()
        assert edit.text == "<b>A useful answer</b>"
    else:
        executor.answer.assert_not_awaited()
        assert edit.text == "That question is empty or too long. Shorten it and try again."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected,button",
    [
        ("private", "Enable free models in Derp settings, or use paid private chat.", "Start personal chat"),
        ("missing", "I couldn't verify this request. Open Derp and try again.", "Start personal chat"),
        ("invalid_id", "I couldn't verify this request. Open Derp and try again.", "Start personal chat"),
        ("invalid_query", "That question is empty or too long. Shorten it and try again.", "Ask another question"),
        ("accounting", "I couldn't verify this request. Open Derp and try again.", "Start personal chat"),
        ("timeout", "That took too long. Try again.", "Ask another question"),
        ("provider", "I couldn't answer that here. Try again.", "Ask another question"),
        ("rejected", "I couldn't answer that question. Try wording it differently.", "Ask another question"),
        ("unusable", "I couldn't produce a useful answer. Try wording it differently.", "Ask another question"),
    ],
)
async def test_native_admission_and_every_failure_outcome_keeps_recovery(case, expected, button):
    store, bot = ReadStore(consented=case != "private", missing=case == "missing"), RecordingBot()
    failure = {
        "timeout": TimeoutError(),
        "provider": RuntimeError("synthetic provider error"),
        "rejected": Rejected(RejectionReason.POLICY),
    }.get(case)
    app, dispatcher, executor, recorder = host(store, output="x" * 2001 if case == "unusable" else "answer", failure=failure)
    if case == "accounting":
        recorder.start.side_effect = RuntimeError("synthetic accounting edge failure")
    event = chosen(query="\x00" if case == "invalid_query" else "question", result_id="malformed" if case == "invalid_id" else None)
    async with app:
        await dispatcher.feed_update(bot, event)
    assert len(bot.requests) == 1
    edit = bot.requests[0]
    assert edit.text == expected and edit.reply_markup.inline_keyboard[0][0].text == button
    assert edit.inline_message_id == "inline-1" and store.active_sessions == 0
    if case in {"private", "missing", "invalid_id", "invalid_query", "accounting"}:
        executor.answer.assert_not_awaited()
    else:
        executor.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_uneditable_selection_has_no_provider_or_telegram_effects():
    store, bot = ReadStore(), RecordingBot()
    app, dispatcher, executor, recorder = host(store)
    async with app:
        await dispatcher.feed_update(bot, chosen(inline_id=None))
    executor.answer.assert_not_awaited()
    recorder.start.assert_not_awaited()
    assert not bot.requests


def test_request_identity_remains_native_and_distinguishes_sent_messages():
    result_id = str(uuid4())
    assert _request_id(USER_ID, result_id, "inline-1") == _inline_request_id(USER_ID, result_id, "inline-1")
    assert _request_id(USER_ID, result_id, "inline-1") != _request_id(USER_ID, result_id, "inline-2")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output", ["**Bold** & <tag>", "😀" * 1999, "&" * 1999, "[link](https://example.invalid/a)", "```python\nprint('x')\n```"]
)
async def test_output_matches_native_sender_for_formatting_astral_and_expansion(output):
    store, bot, native_bot = ReadStore(), RecordingBot(), RecordingBot()
    app, dispatcher, executor, recorder = host(store, output=output)
    async with app:
        await dispatcher.feed_update(bot, chosen())
    actual = bot.requests[-1]
    await MessageSender(native_bot, chat_id=0).edit_inline("inline-1", output, reply_markup=actual.reply_markup)
    assert actual.model_dump() == native_bot.requests[-1].model_dump()


@pytest.mark.asyncio
async def test_native_session_html_fallback_is_retained_without_regeneration():
    rejected = EditMessageText(text="x", inline_message_id="inline-1")
    bot = RecordingBot(session=RecordingSession([TelegramBadRequest(rejected, "can't parse entities"), True]))
    bot.session.middleware(ResilientRequestMiddleware())
    app, dispatcher, executor, recorder = host(ReadStore())
    async with app:
        await dispatcher.feed_update(bot, chosen())
    assert len(bot.requests) == 2
    assert bot.requests[0].parse_mode == "HTML"
    assert bot.requests[1].parse_mode is None and bot.requests[1].text == "A useful answer"
    executor.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_model_projection_blocks_revoked_or_invalid_legacy_privacy():
    store, bot = ReadStore(), RecordingBot()
    app, dispatcher, executor, recorder = host(store)
    async with app:
        await dispatcher.feed_update(bot, chosen())
        store.user.inference_privacy_mode = "invalid-legacy-value"
        await dispatcher.feed_update(bot, chosen(inline_id="inline-2"))
    assert store.reads == 2 and store.active_sessions == 0
    executor.answer.assert_awaited_once()
    assert bot.requests[-1].text == "Enable free models in Derp settings, or use paid private chat."


@pytest.mark.asyncio
async def test_native_dispatcher_factory_preserves_localization_privacy_and_session_fallback(monkeypatch):
    import logfire
    from derp import application
    from derp.application import APPLICATION_ROUTERS, Runtime, create_dispatcher
    from derp.common.update_context import update_ctx
    from derp.config import settings
    from derp.handlers.chat import router as chat_router
    from derp.handlers.inline import router as original
    from derp.handlers.tool_approvals import router as approvals_router
    from derp.middlewares import api_persist, database_logger

    store, bot = ReadStore(), RecordingBot()
    store.db.session.side_effect = store.session
    service, executor, recorder = inline_service(store)
    app = App(InlineAnswers(service))
    replacement = app.build_router()
    monkeypatch.setattr(
        application, "APPLICATION_ROUTERS", tuple(replacement if router is original else router for router in APPLICATION_ROUTERS)
    )
    # Only SQL writes, accounting, provider, telemetry export and Telegram are synthetic.
    # The native factory installs every host middleware with its production settings.
    upsert_user = AsyncMock()
    monkeypatch.setattr(database_logger, "upsert_user", upsert_user)
    inbound_capture, outbound_capture = AsyncMock(), AsyncMock()
    monkeypatch.setattr(database_logger, "upsert_message_from_update", inbound_capture)
    monkeypatch.setattr(api_persist, "upsert_message_from_message", outbound_capture)
    resources = {field.name: MagicMock(name=field.name) for field in fields(Runtime)}
    resources.update(bot=bot, db=store.db, inline_chat_service=service, inference_recorder=recorder)
    runtime = Runtime(**resources)
    telemetry = MagicMock(spec=logfire.Logfire)
    dispatcher = create_dispatcher(runtime, settings, telemetry)
    actor = ACTOR.model_copy(update={"language_code": "ru"})
    inspected = []

    async def inspect_native_context(handler, event, data):
        assert data["db"] is store.db and data["user"] is event.from_user
        assert data["user"].id == actor.id and data["user"].language_code == "ru"
        assert data["event_router"].name == "inline" and data["user_model"] is store.user
        assert I18n.get_current().current_locale == "ru"
        assert update_ctx.get().user_id == actor.id
        assert store.active_sessions == 0
        inspected.append(event)
        return await handler(event, data)

    dispatcher.chosen_inline_result.middleware(inspect_native_context)
    try:
        assert original.parent_router is None
        assert approvals_router.parent_router is chat_router
        assert all(router.parent_router is dispatcher for router in APPLICATION_ROUTERS if router is not original)
        async with app:
            await dispatcher.feed_update(
                bot, Update(update_id=1, inline_query=InlineQuery(id="q", from_user=actor, query="**Synthetic question**", offset=""))
            )
            offer = bot.requests[-1]
            assert isinstance(offer, AnswerInlineQuery)
            article = offer.results[0]
            assert article.title == "Спросить Дерпа"
            assert article.reply_markup.inline_keyboard[0][0].text == "Добавить Дерпа в чат"
            assert offer.button.text == "Открыть личный чат" and offer.button.start_parameter == "start"
            upsert_user.assert_not_awaited()
            assert store.reads == 0
            event = chosen(result_id=article.id, actor=actor)
            # The host's real session middleware retries only the failed HTML edit.
            rejected = EditMessageText(text="x", inline_message_id="inline-1")
            bot.recording.responses.extend([TelegramBadRequest(rejected, "can't parse entities"), True])
            await dispatcher.feed_update(bot, event)
            assert bot.requests[-2].parse_mode == "HTML"
            assert bot.requests[-1].parse_mode is None and bot.requests[-1].text == "A useful answer"
            assert bot.requests[-1].reply_markup.inline_keyboard[0][0].text == "Добавить Дерпа в чат"
            store.user.inference_privacy_mode = InferencePrivacyMode.PRIVATE_ONLY.value
            store.user.free_inference_revoked_at = NOW
            event = chosen(result_id=article.id, inline_id="inline-2", actor=actor)
            await dispatcher.feed_update(bot, event)
            assert bot.requests[-1].text == "Включите бесплатные модели в настройках Дерпа или используйте платный приватный чат."
            assert bot.requests[-1].reply_markup.inline_keyboard[0][0].text == "Открыть личный чат"
        executor.answer.assert_awaited_once()
        recorder.succeed_reports.assert_awaited_once()
        assert store.reads == upsert_user.await_count == len(inspected) == 2
        assert upsert_user.await_args.kwargs["telegram_id"] == actor.id
        assert store.active_sessions == 0 and update_ctx.get() is None
        assert telemetry.span.call_count == 3
        inbound_capture.assert_not_awaited()
        outbound_capture.assert_not_awaited()
    finally:
        await dispatcher.fsm.close()
        await bot.session.close()
        # Native module-level routers live across tests; production attaches them once.
        for router in tuple(dispatcher.sub_routers):
            dispatcher.sub_routers.remove(router)
            router._parent_router = None

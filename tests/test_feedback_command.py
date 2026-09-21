"""Feedback cards authorize their owner and publish the exact confirmed snapshot."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendDocument, SendMessage
from aiogram.types import BufferedInputFile, CallbackQuery, InaccessibleMessage, User

from msu_hub_bot.commands import feedback as commands
from msu_hub_bot.feedback import FeedbackContext, FeedbackDiagnostic, FeedbackError, FeedbackMessage, FeedbackOrigin, FeedbackService
from msu_hub_bot.feedback.presentation import FeedbackCallback, keyboard, render_report, report_caption, report_method, text_size
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker
from msu_hub_bot.storage.features import jobs as feature_jobs
from msu_hub_bot.storage.features import store as feature_store
from msu_hub_bot.telegram.filters import MetaInfo
from quiz_helpers import FeatureFixture
from telegram_helpers import RecordingSession, make_message

NOW = datetime(2030, 1, 1, 10, tzinfo=UTC)
CHAT = -123
TOPIC = 17
DESTINATION = -987654


class FeedbackSession(RecordingSession):
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, (SendMessage, SendDocument, EditMessageText)):
            self.methods.append(method)
            return make_message(
                bot,
                message_id=getattr(method, "message_id", None) or 100 + len(self.methods),
                chat={"id": method.chat_id, "type": "supergroup", "title": "Synthetic chat"},
                from_user={"id": bot.id, "is_bot": True, "first_name": "Bot"},
                message_thread_id=getattr(method, "message_thread_id", None),
                is_topic_message=getattr(method, "message_thread_id", None) is not None,
            )
        return await super().make_request(bot, method, timeout)


def context(*, long=False):
    return FeedbackContext(
        origin=FeedbackOrigin(chat_id=CHAT, thread_id=TOPIC, label="Synthetic origin <&>"),
        reply=FeedbackMessage(chat_id=CHAT, thread_id=TOPIC, message_id=2, sent_at=NOW, author_name="Reply author", text="Selected reply"),
        recent_messages=[
            FeedbackMessage(
                chat_id=CHAT,
                thread_id=TOPIC,
                message_id=index + 3,
                sent_at=NOW,
                author_name="Recent author",
                text="UNSELECTED " + ("Full source text " * 55 if long else "recent context"),
            )
            for index in range(5 if long else 1)
        ],
        diagnostics=[FeedbackDiagnostic(at=NOW, handler="process_roll", outcome="failed", command="roll", message_id=9)],
        diagnostics_since=NOW - timedelta(hours=1),
    )


@pytest.fixture
async def rig(monkeypatch):
    backend = FeatureFixture()
    backend.now = NOW

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return backend.now.replace(tzinfo=None) if tz is None else backend.now.astimezone(tz)

    monkeypatch.setattr(feature_jobs, "datetime", Clock)
    monkeypatch.setattr(feature_store, "datetime", Clock)
    bot = Bot("123456789:" + "a" * 35, session=FeedbackSession())
    store = FeatureStore(backend)
    worker = FeatureWorker(store)
    service = FeedbackService(bot, store, worker, destination_chat_id=DESTINATION, destination_name="Developer feedback")
    service.clock = lambda: backend.now
    capture = AsyncMock(return_value=context())
    monkeypatch.setattr(commands, "capture_context", capture)
    value = SimpleNamespace(bot=bot, backend=backend, service=service, worker=worker, capture=capture, db=SimpleNamespace())
    yield value
    await bot.session.close()


async def card(rig, *, long=False):
    record = await rig.service.create(
        author_id=42,
        author_name="Reporter <&>",
        chat_id=CHAT,
        thread_id=TOPIC,
        source_message_id=20,
        description="A useful <literal> report",
        candidates=context(long=long),
    )
    record = await rig.service.bind(42, record.key, chat_id=CHAT, message_id=100, expected_etag=record.etag)
    message = make_message(
        rig.bot,
        message_id=100,
        chat={"id": CHAT, "type": "supergroup"},
        from_user={"id": rig.bot.id, "is_bot": True, "first_name": "Bot"},
        message_thread_id=TOPIC,
        is_topic_message=True,
    )
    return record, message


async def current(rig, key):
    return await rig.service.get(42, key, ui_chat_id=CHAT, ui_message_id=100)


async def press(rig, record, message, action, value="-", *, author=42, revision=None):
    data = FeedbackCallback(key=record.key, revision=(revision or record.etag).replace("-", ""), action=action, value=value)
    query = CallbackQuery(
        id="query",
        from_user=User(id=author, is_bot=False, first_name="User"),
        message=message,
        data=data.pack(),
        chat_instance="synthetic-instance",
    ).as_(rig.bot)
    await commands.Feedback.process_cb(query, data, rig.service)


async def test_missing_description_gives_usage_without_context_or_fsm(rig):
    message = make_message(rig.bot, text="/feedback")
    await commands.Feedback.process(message, MetaInfo(message, command="feedback"), rig.service, rig.db)
    rig.capture.assert_not_awaited()
    assert not rig.backend.records
    assert isinstance(rig.bot.session.methods[0], SendMessage)
    assert "/feedback описание" in rig.bot.session.methods[0].text


async def test_creation_keeps_trigger_and_discloses_destination_identity_retention_and_defaults(rig):
    message = make_message(
        rig.bot,
        text="/feedback report",
        message_id=20,
        chat={"id": CHAT, "type": "supergroup"},
        message_thread_id=TOPIC,
        is_topic_message=True,
    )
    result = await commands.Feedback.process(message, MetaInfo(message, command="feedback", text="report"), rig.service, rig.db)
    first, last = rig.bot.session.methods
    assert isinstance(first, SendMessage) and first.reply_parameters.message_id == 20 and first.message_thread_id == TOPIC
    assert isinstance(last, EditMessageText) and last.message_id == result.message_id
    assert "Developer feedback" in first.text and "навсегда" in first.text and "Telegram ID (42)" in first.text
    assert "время создания" in first.text and first.parse_mode is None
    data = [FeedbackCallback.unpack(button.callback_data) for row in last.reply_markup.inline_keyboard for button in row]
    assert next(button.value for button in data if button.action == "c") == "0"
    assert next(button.value for button in data if button.action == "r") == "0"
    assert next(button.value for button in data if button.action == "h") == "1"
    assert next(button.value for button in data if button.action == "d") == "0"
    assert not any(button.action == "s" for button in data)
    assert all(len(button.pack().encode()) <= 64 for button in data)
    assert not any(isinstance(method, DeleteMessage) for method in rig.bot.session.methods)
    before = len(rig.bot.session.methods)
    await commands.Feedback.process(message, MetaInfo(message, command="feedback", text="report"), rig.service, rig.db)
    assert len(rig.bot.session.methods) == before


async def test_concurrent_redelivery_publishes_only_one_bound_card(rig, monkeypatch):
    message = make_message(
        rig.bot,
        text="/feedback report",
        message_id=20,
        chat={"id": CHAT, "type": "supergroup"},
        message_thread_id=TOPIC,
        is_topic_message=True,
    )
    entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = rig.bot.session.make_request
    attempted = []

    async def gated(bot, method, timeout=None):
        if isinstance(method, SendMessage):
            attempted.append(method)
            entered.set()
            await release.wait()
        return await original(bot, method, timeout)

    async def invoke(*, second=False):
        if second:
            second_started.set()
        return await commands.Feedback.process(message, MetaInfo(message, command="feedback", text="report"), rig.service, rig.db)

    monkeypatch.setattr(rig.bot.session, "make_request", gated)
    first = asyncio.create_task(invoke())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(invoke(second=True))
        await second_started.wait()
        await asyncio.sleep(0)
        assert len(attempted) == 1
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert results[0] is not None and results[1] is None
        assert sum(isinstance(method, SendMessage) for method in rig.bot.session.methods) == 1
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("action,value", [("k", "i"), ("c", "0"), ("p", "-"), ("s", "-"), ("x", "-")])
async def test_every_action_rejects_another_user_without_publishing_context(rig, action, value):
    record, message = await card(rig)
    await press(rig, record, message, action, value, author=43)
    assert (await current(rig, record.key)).etag == record.etag
    assert not rig.backend.jobs
    assert all(isinstance(method, AnswerCallbackQuery) and method.show_alert for method in rig.bot.session.methods)


@pytest.mark.parametrize("wrong", ["message", "chat", "author", "inaccessible"])
async def test_callback_requires_the_bound_accessible_bot_message(rig, wrong):
    record, message = await card(rig)
    if wrong == "message":
        message = message.model_copy(update={"message_id": 101})
    elif wrong == "chat":
        message = message.model_copy(update={"chat": message.chat.model_copy(update={"id": -999})})
    elif wrong == "author":
        message = message.model_copy(update={"from_user": User(id=42, is_bot=False, first_name="User")})
    else:
        message = InaccessibleMessage(chat=message.chat, message_id=message.message_id).as_(rig.bot)
    await press(rig, record, message, "c", "0")
    assert (await current(rig, record.key)).etag == record.etag
    assert all(isinstance(method, AnswerCallbackQuery) for method in rig.bot.session.methods)


async def test_replayed_checkbox_sets_a_desired_state_once_and_refreshes_stale_buttons(rig):
    record, message = await card(rig)
    await press(rig, record, message, "c", "0")
    changed = await current(rig, record.key)
    assert not changed.value.selection.chat and changed.etag != record.etag
    await press(rig, record, message, "c", "0")
    replayed = await current(rig, record.key)
    assert replayed.etag == changed.etag and not replayed.value.selection.chat
    assert "уже изменилась" in rig.bot.session.methods[-1].text
    buttons = rig.bot.session.methods[-2].reply_markup.inline_keyboard
    data = [FeedbackCallback.unpack(button.callback_data) for row in buttons for button in row]
    assert all(button.revision == changed.etag.replace("-", "") for button in data)


async def test_preview_is_exact_then_submission_is_permanent_and_replay_does_not_duplicate_it(rig):
    record, message = await card(rig)
    report = rig.service.build_report(record)
    assert "Selected reply" in report.rendered_text and "UNSELECTED" not in report.rendered_text
    await press(rig, record, message, "p")
    ready = await current(rig, record.key)
    edits = [method for method in rig.bot.session.methods if isinstance(method, EditMessageText)]
    assert edits[0].text == edits[1].text == report.rendered_text
    assert edits[0].parse_mode is None and edits[0].link_preview_options.is_disabled
    assert ready.value.preview_digest is not None
    await press(rig, ready, message, "s")
    saved = await rig.service.get_report(42, record.key)
    assert saved.value.rendered_text == report.rendered_text and saved.expires_at is None
    jobs = len(rig.backend.jobs)
    await press(rig, ready, message, "s")
    assert len(rig.backend.jobs) == jobs
    assert not any(isinstance(method, (SendMessage, SendDocument)) for method in rig.bot.session.methods)


async def test_changed_selection_invalidates_preview_and_old_submit_button(rig):
    record, message = await card(rig)
    await press(rig, record, message, "p")
    ready = await current(rig, record.key)
    await press(rig, ready, message, "r", "0")
    changed = await current(rig, record.key)
    assert changed.value.preview_digest is None
    await press(rig, ready, message, "s")
    assert not rig.backend.jobs
    assert (await current(rig, record.key)).etag == changed.etag


@pytest.mark.parametrize("units,document", [(4095, False), (4096, False), (4097, True)])
async def test_report_delivery_uses_exact_utf16_boundary_and_full_utf8_document(rig, units, document):
    record, _ = await card(rig)
    report = rig.service.build_report(record)
    body = "😀" * (units // 2) + ("x" if units % 2 else "")
    report.rendered_text = body
    method = report_method(report, DESTINATION)
    assert text_size(body) == units and method.parse_mode is None
    if document:
        assert isinstance(method, SendDocument) and isinstance(method.document, BufferedInputFile)
        assert method.document.data.decode("utf-8") == body and method.document.filename == "report.txt"
        assert text_size(method.caption) <= 1024
    else:
        assert isinstance(method, SendMessage) and method.text == body


async def test_file_caption_stays_useful_and_bounded_with_maximum_unicode_fields(rig):
    record, _ = await card(rig)
    report = rig.service.build_report(record)
    report.description = "Useful summary " + "😀" * 1900
    report.author_name = "Reporter " + "🙂" * 119
    report.destination_name = "Destination " + "🚀" * 88
    caption = report_caption(report)
    assert text_size(caption) <= 1024
    assert "Useful summary" in caption and "Reporter" in caption and "Destination" in caption
    assert "report.txt" in caption


async def test_report_leads_with_description_and_links_only_selected_supergroup_messages(rig):
    record, _ = await card(rig)
    report = rig.service.build_report(record)
    report.context.reply.chat_id = -1001234567890
    report.context.diagnostics[0].release = "synthetic-release"
    rendered = render_report(report)
    assert rendered.index(report.description) < rendered.index("Автор:")
    assert "https://t.me/c/1234567890/2" in rendered
    assert "synthetic-release" in rendered
    report.context.reply.chat_id = 42
    assert "https://t.me/c/" not in render_report(report)


async def test_long_preview_sends_the_complete_file_before_enabling_submit(rig, monkeypatch):
    record, message = await card(rig, long=True)
    record = await rig.service.change(
        42,
        record.key,
        expected_etag=record.etag,
        ui_chat_id=CHAT,
        ui_message_id=100,
        selection=record.value.selection.model_copy(update={"recent": True}),
    )
    expected = rig.service.build_report(record).rendered_text
    assert text_size(expected) > 4096
    original = rig.service.preview

    async def mark(*args, **kwargs):
        sent = rig.bot.session.methods[-1]
        assert isinstance(sent, SendDocument) and sent.document.data.decode("utf-8") == expected
        assert sent.reply_parameters.message_id == 100 and sent.message_thread_id == TOPIC
        return await original(*args, **kwargs)

    monkeypatch.setattr(rig.service, "preview", mark)
    await press(rig, record, message, "p")
    assert (await current(rig, record.key)).value.preview_digest is not None
    assert len([method for method in rig.bot.session.methods if isinstance(method, SendDocument)]) == 1


@pytest.mark.parametrize("long", [False, True])
async def test_failed_preview_delivery_never_authorizes_submission(rig, monkeypatch, long):
    record, message = await card(rig, long=long)
    if long:
        record = await rig.service.change(
            42,
            record.key,
            expected_etag=record.etag,
            ui_chat_id=CHAT,
            ui_message_id=100,
            selection=record.value.selection.model_copy(update={"recent": True}),
        )
    original = rig.bot.session.make_request

    async def fail(bot, method, timeout=None):
        if isinstance(method, (SendDocument, EditMessageText)):
            raise TelegramNetworkError(method=method, message="synthetic uncertain delivery")
        return await original(bot, method, timeout)

    monkeypatch.setattr(rig.bot.session, "make_request", fail)
    await press(rig, record, message, "p")
    assert (await current(rig, record.key)).value.preview_digest is None
    await press(rig, record, message, "s")
    assert not rig.backend.jobs


async def test_change_during_preview_delivery_cannot_authorize_the_old_snapshot(rig, monkeypatch):
    record, message = await card(rig)
    original = rig.bot.session.make_request
    changed = False

    async def concurrent_change(bot, method, timeout=None):
        nonlocal changed
        if isinstance(method, EditMessageText) and not changed:
            changed = True
            await rig.service.change(42, record.key, expected_etag=record.etag, ui_chat_id=CHAT, ui_message_id=100, kind="idea")
        return await original(bot, method, timeout)

    monkeypatch.setattr(rig.bot.session, "make_request", concurrent_change)
    await press(rig, record, message, "p")
    latest = await current(rig, record.key)
    assert latest.value.kind == "idea" and latest.value.preview_digest is None
    assert "уже изменилась" in rig.bot.session.methods[-1].text
    assert not rig.backend.jobs


async def test_stale_refresh_keeps_controls_when_selected_context_has_aged_out(rig):
    rig.capture.return_value = context()
    old = context()
    old.reply.sent_at = NOW - timedelta(days=30) + timedelta(minutes=1)
    record = await rig.service.create(
        author_id=42,
        author_name="Reporter",
        chat_id=CHAT,
        thread_id=TOPIC,
        source_message_id=20,
        description="Report",
        candidates=old,
    )
    record = await rig.service.bind(42, record.key, chat_id=CHAT, message_id=100, expected_etag=record.etag)
    message = make_message(
        rig.bot,
        message_id=100,
        chat={"id": CHAT, "type": "supergroup"},
        from_user={"id": rig.bot.id, "is_bot": True, "first_name": "Bot"},
    )
    await press(rig, record, message, "p")
    ready = await current(rig, record.key)
    rig.backend.now += timedelta(minutes=2)
    await press(rig, record, message, "r", "0")
    edit = rig.bot.session.methods[-2]
    assert isinstance(edit, EditMessageText) and "старше 30 дней" in edit.text
    callbacks = [FeedbackCallback.unpack(button.callback_data) for row in edit.reply_markup.inline_keyboard for button in row]
    assert all(button.revision == ready.etag.replace("-", "") for button in callbacks)
    assert not any(button.action == "s" for button in callbacks)
    await press(rig, ready, message, "r", "0")
    latest = await current(rig, record.key)
    assert not latest.value.selection.reply and latest.value.preview_digest is None


async def test_cancel_discards_draft_and_preserves_the_trigger(rig):
    record, message = await card(rig)
    await press(rig, record, message, "x")
    with pytest.raises(FeedbackError):
        await current(rig, record.key)
    assert not rig.backend.jobs
    assert not any(isinstance(method, DeleteMessage) for method in rig.bot.session.methods)


async def test_unavailable_context_is_visible_and_cannot_gain_a_submit_button(rig):
    record, _ = await card(rig)
    record.value.context.reply = None
    record.value.context.reply_available = False
    record.value.context.recent_messages = []
    record.value.context.recent_available = False
    record.value.context.diagnostics = []
    body = commands.draft_text(record)
    assert "Недавние сообщения недоступны" in body and "Сообщение в ответ: недоступно" in body
    data = [FeedbackCallback.unpack(button.callback_data) for row in keyboard(record).inline_keyboard for button in row]
    assert len([button for button in data if button.action == "n"]) == 3
    assert not any(button.action == "s" for button in data)

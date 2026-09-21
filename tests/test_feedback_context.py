"""Feedback freezes bounded, attributed excerpts and private command diagnostics."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters.command import CommandObject

from msu_hub_bot.feedback.context import DiagnosticBuffer, capture_context
from msu_hub_bot.storage.errors import RepositoryFailure, RepositoryUnavailable
from msu_hub_bot.storage.models import FeedbackMessageRecord
from msu_hub_bot.telegram.middlewares.telemetry import HandlerTelemetryMiddleware
from msu_hub_bot.telemetry import Telemetry
from telegram_helpers import make_message

NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)
CANARY = "PRIVATE_ARGUMENT_AND_EXCEPTION_BODY"


class Repository:
    def __init__(self, rows=(), error=None):
        self.rows, self.error = list(rows), error
        self.calls = []
        self.started = None
        self.release = None

    async def recent_feedback_messages(self, chat_id, **scope):
        self.calls.append((chat_id, scope))
        if self.started is not None:
            self.started.set()
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.rows


def invocation(**changes):
    return make_message(message_id=100, date=NOW, text="/feedback broken", **changes)


def row(message_id=1, **changes):
    return FeedbackMessageRecord(
        chat_id=-1001234567890,
        message_id=message_id,
        sent_at=NOW - timedelta(minutes=1),
        thread_id=None,
        author_id=42,
        author_kind="user",
        author_name="Test user",
        text="Hello",
        media_kind=None,
        truncated=False,
    ).model_copy(update=changes)


async def test_reply_does_not_require_archive_and_ordinary_thread_ids_do_not_create_topics():
    reply = make_message(
        message_id=88,
        date=NOW - timedelta(minutes=1),
        caption="<literal caption>",
        message_thread_id=77,
        photo=[{"file_id": CANARY, "file_unique_id": "file", "width": 1, "height": 1}],
    )
    repository = Repository([row(80, text="Archived")])
    result = await capture_context(invocation(reply_to_message=reply, message_thread_id=88), repository=repository, now=NOW)
    assert result.origin.thread_id is None
    assert result.reply.message_id == 88 and result.reply.text == "<literal caption>"
    assert result.reply.thread_id is None and result.reply.media_kind == "photo"
    assert result.reply.author_name == "Test user" and result.reply.author_kind == "user"
    assert [item.message_id for item in result.recent_messages] == [80]
    assert repository.calls == [(-1001234567890, {"thread_id": None, "before": NOW, "before_message_id": 100})]
    assert CANARY not in result.model_dump_json()


async def test_same_second_prior_message_uses_capture_clock_for_archive_observation_cutoff():
    captured = NOW + timedelta(milliseconds=800)
    repository = Repository([row(99, sent_at=NOW)])
    result = await capture_context(invocation(), repository=repository, now=captured)
    assert repository.calls[0][1]["before"] == captured
    assert [message.message_id for message in result.recent_messages] == [99]


async def test_direct_rich_reply_uses_visible_text_without_hidden_urls_or_media_identifiers():
    reply = make_message(
        message_id=2,
        date=NOW,
        rich_message={
            "blocks": [
                {"type": "paragraph", "text": ["Visible ", {"type": "url", "text": "source", "url": f"https://example.invalid/{CANARY}"}]},
                {
                    "type": "photo",
                    "photo": [{"file_id": CANARY, "file_unique_id": "file", "width": 1, "height": 1}],
                    "caption": {"text": "Caption"},
                },
            ]
        },
    )
    result = await capture_context(invocation(reply_to_message=reply), repository=Repository(), now=NOW)
    assert result.reply.text == "Visible source\n\nCaption"
    assert result.reply.media_kind == "rich_message"
    assert CANARY not in result.model_dump_json()


@pytest.mark.parametrize("age", [timedelta(days=30), timedelta(days=31), timedelta(seconds=-1)])
async def test_expired_or_future_direct_reply_is_visibly_unavailable(age):
    message = invocation(reply_to_message=make_message(message_id=2, date=NOW - age, text=CANARY))
    result = await capture_context(message, repository=Repository(), now=NOW)
    assert result.reply is None and result.reply_available is False
    assert CANARY not in result.model_dump_json()


@pytest.mark.parametrize("scope", [{"business_connection_id": "business"}, {"direct_messages_topic": {"topic_id": 7}}])
async def test_unsupported_scope_never_reads_archive_or_copies_reply(scope):
    message = invocation(**scope, reply_to_message=make_message(message_id=2, date=NOW, text=CANARY))
    repository = Repository()
    result = await capture_context(message, repository=repository, now=NOW)
    assert not result.recent_available and not result.reply_available
    assert not repository.calls and CANARY not in result.model_dump_json()


async def test_storage_failure_is_visible_nonfatal_but_cancellation_propagates():
    repository = Repository(error=RepositoryUnavailable(RepositoryFailure.TIMEOUT))
    result = await capture_context(invocation(), repository=repository, now=NOW)
    assert result.recent_available is False
    assert result.recent_messages == []
    with pytest.raises(asyncio.CancelledError):
        await capture_context(invocation(), repository=Repository(error=asyncio.CancelledError()), now=NOW)


async def test_cross_topic_direct_reply_is_not_context():
    source = make_message(message_id=2, date=NOW, text=CANARY, is_topic_message=True, message_thread_id=9)
    result = await capture_context(
        invocation(reply_to_message=source, is_topic_message=True, message_thread_id=7), repository=Repository(), now=NOW
    )
    assert result.reply is None and not result.reply_available
    assert CANARY not in result.model_dump_json()


async def test_context_is_frozen_before_the_archive_await_and_defends_scope_age():
    stamp = [NOW - timedelta(seconds=1)]
    diagnostics = DiagnosticBuffer(clock=lambda: stamp[0])
    diagnostics.record(make_message(message_id=1), handler="commands.first", command="first", outcome="failed")
    source = make_message(message_id=50, date=NOW - timedelta(minutes=1), text="Original")
    repository = Repository([row(1), row(2, thread_id=99), row(3, chat_id=-555), row(4, sent_at=NOW - timedelta(days=30)), row(101)])
    repository.started, repository.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        capture_context(invocation(reply_to_message=source), repository=repository, diagnostics=diagnostics, now=NOW)
    )
    await repository.started.wait()
    source.__dict__["text"] = "Changed after capture"
    stamp[0] = NOW + timedelta(seconds=1)
    diagnostics.record(make_message(message_id=2), handler="commands.later", command="later", outcome="completed")
    repository.release.set()
    result = await task
    assert result.reply.text == "Original"
    assert [item.handler for item in result.diagnostics] == ["commands.first"]
    assert [item.message_id for item in result.recent_messages] == [1]


async def test_byte_budget_handles_six_unicode_or_escaped_excerpts_and_five_diagnostics():
    payload = '\u0001"😺' * 700
    source = make_message(message_id=1, date=NOW, text=payload, from_user={"id": 2**63 - 1, "is_bot": False, "first_name": payload})
    diagnostics = DiagnosticBuffer(clock=lambda: NOW, release="1" * 70 + ".0.0")
    for index in range(5):
        diagnostics.record(make_message(message_id=index + 1), handler="a" * 128, command="界" * 64, outcome="completed")
    result = await capture_context(
        invocation(reply_to_message=source),
        repository=Repository([row(index + 1, text=payload[:800], author_name=payload[:128]) for index in range(5)]),
        diagnostics=diagnostics,
        now=NOW,
    )
    assert len(result.model_dump_json().encode()) <= 12 * 1024
    assert len(result.recent_messages) == 5 and len(result.diagnostics) == 5
    assert result.reply.truncated and all(item.truncated for item in result.recent_messages)
    assert all(len(item.text) <= 800 for item in [result.reply, *result.recent_messages])


def test_diagnostics_scope_expiry_lru_and_copies_are_bounded():
    tick = [0.0]
    diagnostics = DiagnosticBuffer(max_entries=3, clock=lambda: NOW, monotonic=lambda: tick[0])
    for index in range(3):
        diagnostics.record(make_message(message_id=index + 1), handler="commands.run", command="run", outcome="completed")
    assert len(diagnostics.snapshot(invocation(), before=NOW)) == 3
    other = make_message(message_id=4, from_user={"id": 999, "is_bot": False, "first_name": "Other"})
    diagnostics.record(other, handler="commands.other", command="other", outcome="failed")
    first = diagnostics.snapshot(invocation(), before=NOW)
    assert [item.message_id for item in first] == [2, 3]
    first[0].handler = CANARY
    assert CANARY not in str(diagnostics.snapshot(invocation(), before=NOW))
    assert diagnostics.snapshot(invocation(is_topic_message=True, message_thread_id=5), before=NOW) == []
    assert diagnostics.snapshot(invocation(chat={"id": -55, "type": "supergroup"}), before=NOW) == []
    assert len(diagnostics.snapshot(invocation(message_thread_id=987), before=NOW)) == 2
    tick[0] = 30 * 60
    assert diagnostics.snapshot(invocation(), before=NOW) == []
    assert len(diagnostics._entries) == 0


@pytest.mark.parametrize(
    "result,outcome",
    [
        (None, "completed"),
        (False, "completed"),
        (UNHANDLED, "ignored"),
        (RuntimeError(CANARY), "failed"),
        (asyncio.CancelledError(), "cancelled"),
    ],
)
async def test_shared_middleware_captures_only_command_metadata_without_enabled_telemetry(result, outcome):
    telemetry = Telemetry()
    diagnostics = DiagnosticBuffer(clock=lambda: NOW, release="deadbee")
    middleware = HandlerTelemetryMiddleware(telemetry, diagnostics=diagnostics)

    async def handler(event, data):
        if isinstance(result, BaseException):
            raise result
        return result

    data = {
        "handler": SimpleNamespace(flags={"handler_key": "commands.example"}),
        "command": CommandObject(prefix="/", command="example", args=CANARY),
        "private": CANARY,
    }
    try:
        actual = await middleware(handler, make_message(message_id=9, text=f"/example {CANARY}"), data)
        assert actual is result
    except BaseException as error:
        assert error is result
    items = diagnostics.snapshot(invocation(), before=NOW)
    assert len(items) == 1 and items[0].outcome == outcome and items[0].command == "example"
    assert items[0].release == "deadbee"
    assert CANARY not in items[0].model_dump_json()


async def test_passive_handler_and_invalid_release_are_not_diagnostics():
    diagnostics = DiagnosticBuffer(clock=lambda: NOW, release=CANARY)
    middleware = HandlerTelemetryMiddleware(Telemetry(), diagnostics=diagnostics)

    async def handler(event, data):
        return None

    await middleware(handler, make_message(), {"handler": SimpleNamespace(flags={"handler_key": "commands.passive"})})
    assert diagnostics.snapshot(invocation(), before=NOW) == []
    assert diagnostics.release is None

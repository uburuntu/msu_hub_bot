"""A complete result and its status message have separate delivery outcomes."""

import asyncio
from datetime import UTC, datetime

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendDocument
from aiogram.types import Chat, LinkPreviewOptions, Message, MessageEntity
from aiogram.utils.formatting import Pre, Text, TextLink

from teleforge import complete_response
from teleforge.delivery import DeliveryError, DeliveryProgress, DeliveryTarget, ResponsePolicy
from teleforge.formatting import ResponseError, ResponseLimitError
from teleforge.testing import RecordingBot


def status() -> Message:
    return Message(message_id=20, date=datetime.now(UTC), chat=Chat(id=-100, type="supergroup"), text="Waiting")


SOURCE = DeliveryTarget(chat_id=-100, message_id=7, thread_id=31, business_connection_id="business-a")


async def test_short_completion_edits_native_text_with_explicit_preview_options() -> None:
    bot = RecordingBot()
    preview = LinkPreviewOptions(is_disabled=False, url="https://example.org/art.png")
    completed = await complete_response(
        bot, status(), Pre("<literal>"), overflow_to=SOURCE, overflow_notice="Attached", link_preview_options=preview
    )
    assert not completed.spilled and completed.status_error is None
    assert isinstance(completed.result, Message) and completed.result.message_id == 20
    assert len(bot.requests) == 1
    method = bot.requests[0]
    assert isinstance(method, EditMessageText)
    assert method.text == "<literal>" and method.parse_mode is None
    assert method.entities and method.entities[0].type == "pre"
    assert method.link_preview_options == preview


async def test_overflow_preserves_complete_utf8_links_and_original_reply_topic() -> None:
    bot = RecordingBot()
    text = "🦊" * 3000
    content = Text(Pre(text), "\n", TextLink("Source", url="https://example.org/source"))
    completed = await complete_response(
        bot, status(), content, overflow_to=SOURCE, overflow_notice="Full result attached"
    )
    assert completed.spilled and completed.status_error is None
    sent, edited = bot.requests
    assert isinstance(sent, SendDocument) and sent.document.filename == "result.txt"
    assert (sent.chat_id, sent.message_thread_id, sent.business_connection_id) == (-100, 31, "business-a")
    assert sent.reply_parameters and sent.reply_parameters.message_id == 7
    assert bot.recording.uploads[0]["document"] == (text + "\nSource\n\nLinks:\nhttps://example.org/source").encode()
    assert isinstance(edited, EditMessageText) and edited.message_id == 20 and edited.text == "Full result attached"


async def test_direct_message_topic_is_preserved_on_file() -> None:
    bot = RecordingBot()
    source = DeliveryTarget(chat_id=-100, message_id=7, direct_messages_topic_id=88)
    await complete_response(bot, status(), "x" * 5000, overflow_to=source, overflow_notice="Attached")
    assert bot.requests[0].direct_messages_topic_id == 88


@pytest.mark.parametrize("text", ["", "\ud800"])
async def test_invalid_content_never_chooses_file_fallback(text: str) -> None:
    bot = RecordingBot()
    with pytest.raises(ResponseError):
        await complete_response(bot, status(), text, overflow_to=SOURCE, overflow_notice="Attached")
    assert bot.requests == []


async def test_invalid_entity_and_total_file_notice_budget_fail_before_any_write() -> None:
    bot = RecordingBot()
    with pytest.raises(ResponseError):
        await complete_response(
            bot,
            status(),
            "x" * 5000,
            entities=[MessageEntity(type="bold", offset=4999, length=2)],
            overflow_to=SOURCE,
            overflow_notice="Attached",
        )
    with pytest.raises(ResponseLimitError):
        await complete_response(
            bot,
            status(),
            "x" * 5000,
            overflow_to=SOURCE,
            overflow_notice="Attached",
            policy=ResponsePolicy(max_output_bytes=5005),
        )
    assert bot.requests == []


@pytest.mark.parametrize("overflow", [False, True])
async def test_api_failures_never_activate_another_result_write(overflow: bool) -> None:
    bot = RecordingBot()

    async def reject(bot, method):
        return TelegramBadRequest(method=method, message="Bad Request: rejected")

    bot.recording.responder = reject
    with pytest.raises(DeliveryError) as caught:
        await complete_response(
            bot, status(), "x" * (5000 if overflow else 10), overflow_to=SOURCE, overflow_notice="Attached"
        )
    assert not caught.value.uncertain and len(bot.requests) == 1
    assert isinstance(bot.requests[0], SendDocument if overflow else EditMessageText)


async def test_confirmed_file_survives_failed_status_notice() -> None:
    bot = RecordingBot()

    async def respond(bot, method):
        if isinstance(method, EditMessageText):
            return TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
        return bot.recording._default(bot, method)

    bot.recording.responder = respond
    progress = DeliveryProgress()
    completed = await complete_response(
        bot, status(), "x" * 5000, overflow_to=SOURCE, overflow_notice="Attached", progress=progress
    )
    assert completed.spilled and isinstance(completed.result, Message)
    assert completed.status_error is not None and not completed.status_error.uncertain
    assert progress.phase == "complete" and progress.confirmed == ((-100, completed.result.message_id),)
    assert len(bot.requests) == 2


async def test_unconfirmed_upload_exposes_uncertainty_without_replay() -> None:
    bot = RecordingBot()
    bot.recording.responses.append(TimeoutError())
    progress = DeliveryProgress()
    with pytest.raises(DeliveryError) as caught:
        await complete_response(
            bot, status(), "x" * 5000, overflow_to=SOURCE, overflow_notice="Attached", progress=progress
        )
    assert caught.value.uncertain and progress.uncertain
    assert len(bot.requests) == 1 and isinstance(bot.requests[0], SendDocument)


@pytest.mark.parametrize("during_notice", [False, True])
async def test_cancellation_preserves_whether_the_result_was_confirmed(during_notice: bool) -> None:
    bot = RecordingBot()
    waiting = asyncio.Event()

    async def respond(bot, method):
        if isinstance(method, EditMessageText if during_notice else SendDocument):
            waiting.set()
            await asyncio.Event().wait()
        return bot.recording._default(bot, method)

    bot.recording.responder = respond
    progress = DeliveryProgress()
    task = asyncio.create_task(
        complete_response(bot, status(), "x" * 5000, overflow_to=SOURCE, overflow_notice="Attached", progress=progress)
    )
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(bot.requests) == (2 if during_notice else 1)
    assert bool(progress.confirmed) is during_notice
    assert progress.uncertain is not during_notice
    assert progress.phase == ("complete" if during_notice else "cancelled")

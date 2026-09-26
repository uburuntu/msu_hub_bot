"""Synthetic dispatch checks for typed inputs, identities and application-owned effects."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from aiogram.methods import AnswerInlineQuery, AnswerPreCheckoutQuery, EditMessageText, SendMessage
from aiogram.types import (
    Chat,
    ChosenInlineResult,
    Document,
    InlineQuery,
    Message,
    PhotoSize,
    PreCheckoutQuery,
    SuccessfulPayment,
    Voice,
    Update,
    User,
)
from pydantic import ValidationError
from teleforge import App
from teleforge.jobs import JobHandler, bind_jobs
from teleforge.testing import RecordingBot

from teleforge_service_fixtures import Assistant, NativeMedia, RequestScope, create_app

ACTOR = User(id=700, is_bot=False, first_name="User")


def incoming(text: str | None = None, **fields: object) -> Message:
    data: dict[str, object] = {
        "message_id": 20,
        "date": datetime.now(UTC),
        "chat": Chat(id=-100321, type="supergroup"),
        "from_user": ACTOR,
        "text": text,
        "message_thread_id": 17,
        "is_topic_message": True,
        "business_connection_id": "example-business",
    }
    data.update(fields)
    return Message.model_validate(data)


class Answers:
    def __init__(self) -> None:
        self.calls: list[tuple[str, RequestScope]] = []
        self.inputs: list[tuple[Message | None, NativeMedia | None, Message | None, Sequence[object]]] = []

    async def answer(
        self,
        prompt: str,
        scope: RequestScope,
        *,
        message: Message | None = None,
        media: NativeMedia | None = None,
        media_message: Message | None = None,
        recent_messages: Sequence[object] = (),
    ) -> str:
        self.calls.append((prompt, scope))
        self.inputs.append((message, media, media_message, recent_messages))
        return f"Answer to: {prompt}"


class Payments:
    """Application test double, deliberately independent of TeleForge's job binder."""

    def __init__(self) -> None:
        self.rejection: str | None = None
        self.queries: list[PreCheckoutQuery] = []
        self.updates: list[Message] = []
        self.receipts: dict[str, SuccessfulPayment] = {}
        self.outbox: list[dict[str, object]] = []
        self.recovery_calls: list[str] = []
        self.completed: set[str] = set()
        self.fulfilled: list[str] = []
        self.transaction = asyncio.Lock()

    async def check(self, query: PreCheckoutQuery) -> str | None:
        self.queries.append(query)
        return self.rejection

    async def accept(self, message: Message) -> None:
        self.updates.append(message)
        payment = message.successful_payment
        assert payment is not None
        receipt = payment.telegram_payment_charge_id
        # A production service must put both operations in its database transaction.
        async with self.transaction:
            if receipt in self.receipts:
                return
            self.receipts[receipt] = payment
            self.outbox.append({"receipt_id": receipt})

    async def recover(self, receipt_id: str) -> None:
        self.recovery_calls.append(receipt_id)
        async with self.transaction:
            if receipt_id in self.completed:
                return
            if receipt_id not in self.receipts:
                raise LookupError("Application receipt not found")
            self.fulfilled.append(receipt_id)
            self.completed.add(receipt_id)


class Worker:
    def __init__(self) -> None:
        self.handlers: dict[str, JobHandler] = {}

    def register(self, name: str, handler: JobHandler) -> None:
        self.handlers[name] = handler


async def test_text_creation_preserves_actor_request_and_topic_with_native_reply() -> None:
    answers, payments, bot = Answers(), Payments(), RecordingBot()
    async with create_app(answers, payments) as app:
        await app.feed_update(bot, Update(update_id=1, message=incoming("/ask explain <b>this</b>")))
    assert answers.calls == [
        ("explain <b>this</b>", RequestScope("message:42:example-business:-100321:20", 700, -100321, 17, "example-business"))
    ]
    sent = [method for method in bot.requests if isinstance(method, SendMessage)]
    assert len(sent) == 1
    assert sent[0].text == "Answer to: explain <b>this</b>"
    assert sent[0].parse_mode is None
    assert sent[0].message_thread_id == 17
    assert not payments.updates


@dataclass(frozen=True)
class LoadedHistory:
    messages: tuple[object, ...]


@pytest.mark.parametrize(
    "attachment",
    [
        {"document": Document(file_id="pdf", file_unique_id="pdf-1", mime_type="application/pdf")},
        {"voice": Voice(file_id="voice", file_unique_id="voice-1", duration=4, mime_type="audio/ogg")},
        {"photo": [PhotoSize(file_id="photo", file_unique_id="photo-1", width=64, height=64)]},
    ],
)
async def test_contextual_followup_keeps_native_media_history_and_current_reply_target(attachment: dict[str, object]) -> None:
    answers, bot = Answers(), RecordingBot()
    source = incoming(None, message_id=9, **attachment)
    message = incoming("/ask explain the previous answer using this", reply_to_message=source)
    # Opaque native model messages are selected by the host history service.
    # The adapter must not turn them into strings, drop tools, or choose history.
    native_model_message = object()
    history = LoadedHistory((native_model_message,))
    async with create_app(answers, Payments()) as app:
        await app.feed_update(bot, Update(update_id=1, message=message), history=history)
    received, media, media_message, recent = answers.inputs[0]
    assert received is not None and received.message_id == 20
    assert media is not None and media.file_id in {"pdf", "voice", "photo"}
    assert media_message is not None and media_message.message_id == 9
    assert recent is history.messages and recent[0] is native_model_message
    sent = next(method for method in bot.requests if isinstance(method, SendMessage))
    assert sent.reply_parameters is not None and sent.reply_parameters.message_id == 20
    assert sent.message_thread_id == 17 and sent.business_connection_id == "example-business"
    assert all(method.__api_method__ != "getFile" for method in bot.requests)


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize(
    "attachment",
    [
        {"photo": [PhotoSize(file_id="photo", file_unique_id="photo-1", width=64, height=64)]},
        {"voice": Voice(file_id="voice", file_unique_id="voice-1", duration=4, mime_type="audio/ogg")},
        {"document": Document(file_id="pdf", file_unique_id="pdf-1", mime_type="application/pdf")},
    ],
)
async def test_media_only_ask_reaches_answer_service_with_empty_prompt(attachment: dict[str, object], attached: bool) -> None:
    answers, bot = Answers(), RecordingBot()
    source = incoming(None, message_id=9, **attachment)
    message = incoming(None, caption="/ask", **attachment) if attached else incoming("/ask", reply_to_message=source)
    async with create_app(answers, Payments()) as app:
        await app.feed_update(bot, Update(update_id=1, message=message))
    assert len(answers.calls) == 1 and answers.calls[0][0] == ""
    current, media, selected, _history = answers.inputs[0]
    assert current is not None and current.message_id == 20
    assert media is not None and media.file_id in {"photo", "voice", "pdf"}
    assert selected is not None and selected.message_id == (20 if attached else 9)
    response = next(method for method in bot.requests if isinstance(method, SendMessage))
    assert response.reply_parameters is not None and response.reply_parameters.message_id == 20
    assert response.message_thread_id == 17 and response.business_connection_id == "example-business"


async def test_empty_ask_without_media_uses_translated_application_guidance() -> None:
    answers, bot = Answers(), RecordingBot()
    translations = {
        "Send a prompt or reply to the text or media you want to use.": "Напишите вопрос или ответьте на текст, картинку или голосовое."
    }
    async with App(Assistant(answers, translate=lambda text: translations.get(text, text))) as app:
        await app.feed_update(bot, Update(update_id=1, message=incoming("/ask")))
    assert not answers.calls
    assert isinstance(bot.requests[-1], SendMessage)
    assert bot.requests[-1].text == translations["Send a prompt or reply to the text or media you want to use."]


async def test_attached_media_precedes_replied_media() -> None:
    answers, bot = Answers(), RecordingBot()
    source = incoming(None, message_id=9, photo=[PhotoSize(file_id="replied", file_unique_id="r", width=64, height=64)])
    message = incoming(
        None,
        caption="/ask",
        reply_to_message=source,
        photo=[PhotoSize(file_id="attached", file_unique_id="a", width=64, height=64)],
    )
    async with create_app(answers, Payments()) as app:
        await app.feed_update(bot, Update(update_id=1, message=message))
    assert answers.inputs[0][1].file_id == "attached"
    assert answers.inputs[0][2].message_id == 20


async def test_inline_offer_does_not_generate_until_selected_and_edits_inline_identity() -> None:
    answers, payments, bot = Answers(), Payments(), RecordingBot()
    async with create_app(answers, payments) as app:
        await app.feed_update(
            bot, Update(update_id=1, inline_query=InlineQuery(id="inline-query", from_user=ACTOR, query="explain stars", offset=""))
        )
        assert not answers.calls
        assert isinstance(bot.requests[-1], AnswerInlineQuery)
        await app.feed_update(
            bot,
            Update(
                update_id=2,
                chosen_inline_result=ChosenInlineResult(
                    result_id="answer", from_user=ACTOR, query="explain stars", inline_message_id="chosen-inline-message"
                ),
            ),
        )
    assert answers.calls == [("explain stars", RequestScope("inline:42:700:chosen-inline-message", 700))]
    edit = bot.requests[-1]
    assert isinstance(edit, EditMessageText)
    assert edit.inline_message_id == "chosen-inline-message"
    assert edit.chat_id is None and edit.message_id is None


@pytest.mark.parametrize("result_id,inline_id", [("unrelated-result", "inline"), ("answer", None)])
async def test_unrelated_or_uneditable_inline_result_does_not_call_provider(result_id: str, inline_id: str | None) -> None:
    answers, bot = Answers(), RecordingBot()
    async with create_app(answers, Payments()) as app:
        await app.feed_update(
            bot,
            Update(
                update_id=1,
                chosen_inline_result=ChosenInlineResult(
                    result_id=result_id, from_user=ACTOR, query="a question", inline_message_id=inline_id
                ),
            ),
        )
    assert not answers.calls and not bot.requests


@pytest.mark.parametrize("reason", [None, "This invoice is no longer available."])
async def test_native_precheckout_facts_and_service_decision_are_preserved(reason: str | None) -> None:
    payments, bot = Payments(), RecordingBot()
    payments.rejection = reason
    native = PreCheckoutQuery(id="checkout", from_user=ACTOR, currency="XTR", total_amount=7, invoice_payload="application-owned-invoice")
    async with create_app(Answers(), payments) as app:
        await app.feed_update(bot, Update(update_id=1, pre_checkout_query=native))
    assert len(payments.queries) == 1
    assert payments.queries[0].model_dump() == native.model_dump()
    answer = bot.requests[-1]
    assert isinstance(answer, AnswerPreCheckoutQuery)
    assert answer.pre_checkout_query_id == "checkout"
    assert answer.ok is (reason is None) and answer.error_message == reason
    assert not payments.receipts and not payments.outbox


async def test_duplicate_payment_updates_and_recovery_rely_on_application_idempotency() -> None:
    payments, worker, bot = Payments(), Worker(), RecordingBot()
    app = create_app(Answers(), payments)
    assert bind_jobs(app, worker) == ("test.commerce.recover-payment",)
    assert not payments.outbox
    native = SuccessfulPayment(
        currency="XTR",
        total_amount=7,
        invoice_payload="application-owned-invoice",
        telegram_payment_charge_id="receipt-1",
        provider_payment_charge_id="provider-ref",
    )
    async with app:
        update_message = incoming(None, successful_payment=native)
        await app.feed_update(bot, Update(update_id=1, message=update_message))
        await app.feed_update(bot, Update(update_id=2, message=update_message))
        assert len(payments.updates) == 2  # The framework does not pretend it deduplicated settlement.
        assert set(payments.receipts) == {"receipt-1"}
        assert payments.receipts["receipt-1"].model_dump() == native.model_dump()
        assert payments.outbox == [{"receipt_id": "receipt-1"}]
        recover = worker.handlers["test.commerce.recover-payment"]
        await recover(payments.outbox[0])
        await recover(payments.outbox[0])
    assert payments.recovery_calls == ["receipt-1", "receipt-1"]
    assert payments.fulfilled == ["receipt-1"]
    assert not bot.requests


async def test_invalid_recovery_payload_cannot_reach_application_service() -> None:
    payments, worker = Payments(), Worker()
    bind_jobs(create_app(Answers(), payments), worker)
    with pytest.raises(ValidationError):
        await worker.handlers["test.commerce.recover-payment"]({"receipt_id": "receipt", "actor_id": 700})
    assert not payments.recovery_calls

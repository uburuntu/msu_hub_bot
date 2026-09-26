"""Synthetic features for package input/context/payment boundary tests."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from aiogram import F
from aiogram.methods import AnswerInlineQuery, AnswerPreCheckoutQuery
from aiogram.types import (
    Animation,
    Audio,
    ChosenInlineResult,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    PhotoSize,
    PreCheckoutQuery,
    Sticker,
    Video,
    VideoNote,
    Voice,
)
from pydantic import BaseModel, ConfigDict, Field
from teleforge import (
    App,
    Context,
    Feature,
    MediaInput,
    MessageContext,
    TextInput,
    chosen_inline_result,
    command,
    event,
    inline_query,
    job,
    pre_checkout_query,
)

type NativeMedia = PhotoSize | Document | Sticker | Video | Animation | VideoNote | Audio | Voice


class ContextHistory(Protocol):
    """Already-selected model messages for this simple text-response example."""

    @property
    def messages(self) -> Sequence[object]: ...


@dataclass(frozen=True, slots=True)
class RequestScope:
    """Opaque test identity; the injected application decides permissions."""

    request_key: str
    actor_id: int
    chat_id: int | None = None
    thread_id: int | None = None
    business_connection_id: str | None = None


def _scope(ctx: MessageContext) -> RequestScope:
    if ctx.user is None:
        raise ValueError("An identified sender is required after access validation")
    message = ctx.event
    return RequestScope(
        request_key=f"message:{ctx.bot.id}:{message.business_connection_id or '-'}:{message.chat.id}:{message.message_id}",
        actor_id=ctx.user.id,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id if message.is_topic_message else None,
        business_connection_id=message.business_connection_id,
    )


class AnswerService(Protocol):
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
        """Example text-response service with opaque application-owned history.

        The host selects recent context within its topic/privacy/token budget;
        this adapter neither serializes a prompt transcript nor downloads media.
        Inline input has no chat or history. This fixture is not a port of a
        production chat handler, accounting service or output pipeline.
        """
        ...


class Assistant(Feature, key="test.answers"):
    def __init__(self, answers: AnswerService, *, translate: Callable[[str], str] = str) -> None:
        self.answers = answers
        self.tr = translate

    @command("ask", prompt=TextInput(reply=True), media=MediaInput(reply=True))
    async def ask(
        self, ctx: MessageContext, prompt: str = "", media: NativeMedia | None = None, *, history: ContextHistory | None = None
    ) -> str | None:
        if ctx.user is None:
            await ctx.guide(self.tr("I couldn't verify your account. Try again."))
            return None
        if not prompt.strip() and media is None:
            await ctx.guide(self.tr("Send a prompt or reply to the text or media you want to use."))
            return None
        # Reply to the invocation even when its image or context came from a reply.
        ctx.response_target = ctx.event
        return await self.answers.answer(
            prompt,
            _scope(ctx),
            message=ctx.event,
            media=media,
            media_message=ctx.input_sources.get("media"),
            recent_messages=history.messages if history is not None else (),
        )

    @inline_query()
    async def offer_inline(self, ctx: Context, event: InlineQuery) -> AnswerInlineQuery:
        if not event.query.strip():
            return event.answer([], cache_time=0, is_personal=True)
        article = InlineQueryResultArticle(
            id="answer",
            title=self.tr("Ask a question"),
            input_message_content=InputTextMessageContent(message_text=self.tr("Preparing your answer…"), parse_mode=None),
            # Telegram supplies inline_message_id for the selected result with a keyboard.
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=self.tr("Ask another"), switch_inline_query_current_chat="")]]
            ),
        )
        return event.answer([article], cache_time=0, is_personal=True)

    @chosen_inline_result()
    async def answer_inline(self, ctx: Context, event: ChosenInlineResult) -> None:
        result = event
        if result.result_id != "answer" or not result.inline_message_id or not result.query.strip():
            return
        scope = RequestScope(
            request_key=f"inline:{ctx.bot.id}:{result.from_user.id}:{result.inline_message_id}",
            actor_id=result.from_user.id,
        )
        answer = await self.answers.answer(result.query, scope)
        await ctx.edit(answer, kind="text")


class RecoverPayment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    receipt_id: str = Field(min_length=1)


class Payments(Protocol):
    async def check(self, query: PreCheckoutQuery) -> str | None:
        """Return a rejection explanation, or None; validate the native invoice facts."""
        ...

    async def accept(self, message: Message) -> None:
        """Atomically deduplicate payment and enqueue recovery in the application."""
        ...

    async def recover(self, receipt_id: str) -> None:
        """Recover from application state; the worker owns its claims and retries."""
        ...


class Commerce(Feature, key="test.commerce"):
    def __init__(self, payments: Payments) -> None:
        self.payments = payments

    @pre_checkout_query()
    async def checkout(self, ctx: Context, event: PreCheckoutQuery) -> AnswerPreCheckoutQuery:
        reason = await self.payments.check(event)
        return event.answer(ok=reason is None, error_message=reason)

    @event("message", F.successful_payment)
    async def paid(self, ctx: MessageContext, message: Message) -> None:
        await self.payments.accept(message)

    @job("recover-payment", payload=RecoverPayment)
    async def recover_payment(self, payload: RecoverPayment) -> None:
        await self.payments.recover(payload.receipt_id)


def create_app(answers: AnswerService, payments: Payments) -> App:
    """Build only synthetic input/context/payment fixtures."""
    return App(Assistant(answers), Commerce(payments))

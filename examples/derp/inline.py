"""Complete PR29 inline routing over its concrete consent/accounting service."""

import uuid

from aiogram import Bot, F, html
from aiogram.methods import AnswerInlineQuery
from aiogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
)
from aiogram.utils.i18n import gettext as _
from derp.common.sender import MessageSender
from derp.config import settings
from derp.features.inline_chat import (
    InlineChatCompleted,
    InlineChatFailed,
    InlineChatFailureReason,
    InlineChatFeatureService,
    InlineChatInvalid,
    InlineChatInvocation,
    InlineChatOutcome,
)
from derp.inference.privacy import project_inference_privacy
from derp.models import User as UserModel
from derp.observability import report_exception
from teleforge import Feature, chosen_inline_result, inline_query

_REQUEST_NAMESPACE = uuid.UUID("a14fc0e4-cc5c-4d91-ae24-8aa260b680de")


def _request_id(user_id: uuid.UUID, result_id: str, inline_message_id: str) -> uuid.UUID:
    if not isinstance(user_id, uuid.UUID):
        raise TypeError("user_id must be a UUID")
    template_id = uuid.UUID(result_id)
    if not isinstance(inline_message_id, str) or not inline_message_id.strip():
        raise ValueError("inline_message_id must not be blank")
    return uuid.uuid5(_REQUEST_NAMESPACE, f"{user_id}:{template_id}:{inline_message_id}")


def _add_to_chat() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_("Add Derp to your chat"), url=f"https://t.me/{settings.bot_username}?startgroup=true")]
        ]
    )


def _start_personal_chat() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_("Start personal chat"), url=f"https://t.me/{settings.bot_username}?start=inline")]]
    )


def _retry() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_("Ask another question"), switch_inline_query_current_chat="")]]
    )


def _unverified() -> tuple[str, InlineKeyboardMarkup]:
    return _("I couldn't verify this request. Open Derp and try again."), _start_personal_chat()


def _presentation(outcome: InlineChatOutcome) -> tuple[str, InlineKeyboardMarkup]:
    if isinstance(outcome, InlineChatCompleted):
        return outcome.text, _add_to_chat()
    if isinstance(outcome, InlineChatInvalid):
        return _("That question is empty or too long. Shorten it and try again."), _retry()
    if isinstance(outcome, InlineChatFailed):
        text = {
            InlineChatFailureReason.FREE_MODE_REQUIRED: _("Enable free models in Derp settings, or use paid private chat."),
            InlineChatFailureReason.ACCOUNTING_UNAVAILABLE: _("I couldn't verify this request. Open Derp and try again."),
            InlineChatFailureReason.PROVIDER_TIMEOUT: _("That took too long. Try again."),
            InlineChatFailureReason.PROVIDER_REJECTED: _("I couldn't answer that question. Try wording it differently."),
            InlineChatFailureReason.UNUSABLE_OUTPUT: _("I couldn't produce a useful answer. Try wording it differently."),
        }.get(outcome.reason, _("I couldn't answer that here. Try again."))
        recovery = (
            _start_personal_chat()
            if outcome.reason in {InlineChatFailureReason.ACCOUNTING_UNAVAILABLE, InlineChatFailureReason.FREE_MODE_REQUIRED}
            else _retry()
        )
        return text, recovery
    raise TypeError(f"unsupported inline outcome: {type(outcome).__name__}")


class InlineAnswers(Feature, key="inline"):
    """Replace the native inline router; its name preserves native model loading."""

    def __init__(self, service: InlineChatFeatureService) -> None:
        self.service = service

    @inline_query(F.query == "")
    @inline_query(F.query != "")
    async def offer(self, event: InlineQuery) -> AnswerInlineQuery:
        preview = event.query[:200] or "..."
        description = _("Ask Derp: {user_input}").format(user_input=preview) if event.query else _("Ask a question in this chat.")
        prompt = _("Derp is thinking about: {user_input}").format(user_input=preview) if event.query else _("Type a question for Derp.")
        result = InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=_("Ask Derp"),
            description=description,
            input_message_content=InputTextMessageContent(message_text=html.italic(prompt)),
            reply_markup=_add_to_chat(),
        )
        button = InlineQueryResultsButton(text=_("Start personal chat"), start_parameter="start") if event.query else None
        return event.answer([result], button=button, cache_time=300, is_personal=True)

    @chosen_inline_result()
    async def answer(self, chosen_result: ChosenInlineResult, bot: Bot, *, user_model: UserModel | None = None) -> None:
        if not chosen_result.inline_message_id:
            return
        if user_model is None:
            text, markup = _unverified()
        else:
            try:
                request_id = _request_id(user_model.id, chosen_result.result_id, chosen_result.inline_message_id)
            except TypeError, ValueError, AttributeError:
                text, markup = _unverified()
            else:
                try:
                    outcome = await self.service.answer(
                        InlineChatInvocation(request_id, user_model.id, chosen_result.query, project_inference_privacy(user_model))
                    )
                except Exception as exc:
                    report_exception("inline_handler_failed", exception=exc, telegram_user_id=chosen_result.from_user.id)
                    text, markup = _("I couldn't answer that here. Try again."), _retry()
                else:
                    text, markup = _presentation(outcome)
        # Keep Derp's Markdown/HTML conversion and fixed-inline fallback intact.
        await MessageSender(bot=bot, chat_id=0).edit_inline(chosen_result.inline_message_id, text, reply_markup=markup)

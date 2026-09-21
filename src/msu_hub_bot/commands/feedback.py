"""Owned feedback cards: choose context, inspect the snapshot, then submit."""

import asyncio
import re
from typing import TypedDict
from uuid import UUID

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message, ReplyParameters

from msu_hub_bot.commands.quiz_view import compact
from msu_hub_bot.feedback import FeedbackDraft, FeedbackError, FeedbackReport, FeedbackService
from msu_hub_bot.feedback.context import DiagnosticBuffer, capture_context
from msu_hub_bot.feedback.presentation import (
    KIND_VALUES,
    MESSAGE_LIMIT,
    FeedbackCallback,
    draft_text,
    keyboard,
    report_caption,
    report_method,
    text_size,
)
from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.errors import RepositoryError
from msu_hub_bot.storage.features import Conflict, FeatureError, Record
from msu_hub_bot.telegram.callbacks import CallbackCommandBase
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.filters import MetaInfo

HELP = (
    "Есть ошибка или идея? Напиши /feedback описание.\n"
    "Например: /feedback погода показывает вчерашний прогноз\n\n"
    "Покажу получателя и предложу выбрать контекст. Ничего не отправлю без предпросмотра и подтверждения."
)
_SELECTIONS = {"c": "chat", "r": "reply", "h": "recent", "d": "diagnostics"}


class _Guard(TypedDict):
    ui_chat_id: int
    ui_message_id: int


async def _answer(query: CallbackQuery, text: str, *, alert: bool = False) -> None:
    try:
        async with asyncio.timeout(15):
            await bot_for(query)(query.answer(compact(text, 200), show_alert=alert), request_timeout=15)
    except TelegramAPIError, TimeoutError:
        # A lost callback acknowledgement must not undo a committed action.
        pass


async def _edit(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        async with asyncio.timeout(15):
            await bot_for(message)(
                message.edit_text(text, parse_mode=None, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True)),
                request_timeout=15,
            )
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message.casefold():
            raise


async def _refresh(message: Message, record: Record[FeedbackDraft], feedback: FeedbackService) -> None:
    allow_submit = True
    if record.value.preview_digest is None:
        text = draft_text(record)
    else:
        try:
            report = feedback.build_report(record)
        except FeedbackError as error:
            # A selected message may cross its age limit while the draft lives.
            # Keep current controls available so the owner can deselect it.
            text, allow_submit = draft_text(record) + "\n\n" + str(error), False
        else:
            text = report.rendered_text if text_size(report.rendered_text) <= MESSAGE_LIMIT else report_caption(report)
    await _edit(message, text, keyboard(record, allow_submit=allow_submit))


async def _show_preview(message: Message, record: Record[FeedbackDraft], feedback: FeedbackService) -> None:
    report = feedback.build_report(record)
    if text_size(report.rendered_text) <= MESSAGE_LIMIT:
        await _edit(message, report.rendered_text, keyboard(record))
    else:
        method = report_method(report, message.chat.id).model_copy(
            update={
                "reply_parameters": ReplyParameters(message_id=message.message_id),
                "message_thread_id": message.message_thread_id if message.is_topic_message else None,
                "business_connection_id": message.business_connection_id,
            }
        )
        async with asyncio.timeout(15):
            await bot_for(message)(method, request_timeout=15)
    # A failed or uncertain Telegram send leaves this revision ineligible to submit.
    ready = await feedback.preview(
        record.value.author_id,
        record.key,
        expected_etag=record.etag,
        ui_chat_id=message.chat.id,
        ui_message_id=message.message_id,
    )
    await _refresh(message, ready, feedback)


def _submitted(report: FeedbackReport) -> str:
    statuses = {
        "queued": "Отзыв в очереди на доставку.",
        "sending": "Отправляю отзыв.",
        "sent": "Отзыв доставлен.",
        "uncertain": "Доставка не подтверждена. Автоматического повтора не будет — разработчик проверит её отдельно.",
        "failed": "Доставить отзыв не удалось. Он сохранён, разработчик сможет проверить доставку.",
    }
    return f"Спасибо! Отзыв {report.report_id} сохранён.\nПолучатель: {report.destination_name}.\n{statuses[report.status]}"


class Feedback(CallbackCommandBase):
    callback_data = FeedbackCallback

    @staticmethod
    async def process(
        message: Message,
        meta: MetaInfo,
        feedback: FeedbackService,
        db: BotRepository,
        feedback_diagnostics: DiagnosticBuffer | None = None,
    ) -> Message | None:
        try:
            if message.from_user is None or message.from_user.is_bot or message.sender_chat is not None:
                raise FeedbackError("Напиши /feedback от личного аккаунта: тогда только ты сможешь менять и отправлять свой отзыв.")
            description = meta.text.strip()
            if not description:
                body = HELP
            else:
                if len(description) > 2000:
                    raise FeedbackError("Описание слишком длинное: оставь до 2000 символов.")
                if not feedback.destination_chat_id:
                    raise FeedbackError("Обратная связь пока не настроена. Попробуй позже.")
                async with Feedback.lock(("create", message.from_user.id)):
                    candidates = await capture_context(message, repository=db, diagnostics=feedback_diagnostics)
                    record = await feedback.create(
                        author_id=message.from_user.id,
                        author_name=compact(message.from_user.full_name, 128),
                        chat_id=message.chat.id,
                        thread_id=message.message_thread_id if message.is_topic_message else None,
                        source_message_id=message.message_id,
                        description=description,
                        candidates=candidates,
                    )
                    if record.value.ui_message_id is not None:
                        return None  # Redelivery of the same trigger must not create another card.
                    async with asyncio.timeout(15):
                        sent = await bot_for(message)(
                            message.reply(draft_text(record), parse_mode=None, link_preview_options=LinkPreviewOptions(is_disabled=True)),
                            request_timeout=15,
                        )
                    bound = await feedback.bind(
                        message.from_user.id,
                        record.key,
                        chat_id=sent.chat.id,
                        message_id=sent.message_id,
                        expected_etag=record.etag,
                    )
                    await _refresh(sent, bound, feedback)
                    return sent
        except FeedbackError as error:
            body = str(error)
        except FeatureError, RepositoryError, TimeoutError, TelegramAPIError:
            body = "Не удалось открыть карточку отзыва. Попробуй /feedback ещё раз; без подтверждения ничего не отправится."
        async with asyncio.timeout(15):
            return await bot_for(message)(message.reply(body, parse_mode=None), request_timeout=15)

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: FeedbackCallback, feedback: FeedbackService) -> None:
        message = query.message
        if (
            not isinstance(message, Message)
            or message.from_user is None
            or not message.from_user.is_bot
            or message.from_user.id != bot_for(query).id
            or query.from_user.is_bot
        ):
            await _answer(query, "Эта карточка недоступна. Создай новый отзыв через /feedback.", alert=True)
            return
        if re.fullmatch(r"[a-f0-9]{16}", callback_data.key) is None or re.fullmatch(r"[a-f0-9]{32}", callback_data.revision) is None:
            await _answer(query, "Кнопка не распознана. Открой действующую карточку отзыва.", alert=True)
            return
        revision = str(UUID(hex=callback_data.revision))
        guard: _Guard = {"ui_chat_id": message.chat.id, "ui_message_id": message.message_id}
        author_id = query.from_user.id
        async with cls.lock(message):
            try:
                if callback_data.action == "s" and callback_data.value == "-":
                    report = await feedback.submit(author_id, callback_data.key, expected_etag=revision, **guard)
                    await _edit(message, _submitted(report.value))
                    await _answer(query, "Отзыв сохранён")
                    return
                record = await feedback.get(author_id, callback_data.key, **guard)
                if record.etag != revision:
                    raise Conflict()
                if callback_data.action == "k" and callback_data.value in KIND_VALUES:
                    record = await feedback.change(
                        author_id, record.key, expected_etag=revision, kind=KIND_VALUES[callback_data.value], **guard
                    )
                elif callback_data.action in _SELECTIONS and callback_data.value in {"0", "1"}:
                    field = _SELECTIONS[callback_data.action]
                    selection = record.value.selection.model_copy(update={field: callback_data.value == "1"}, deep=True)
                    record = await feedback.change(author_id, record.key, expected_etag=revision, selection=selection, **guard)
                elif callback_data.action == "p" and callback_data.value == "-":
                    await _show_preview(message, record, feedback)
                    await _answer(query, "Проверь точный состав перед отправкой")
                    return
                elif callback_data.action == "x" and callback_data.value == "-":
                    await feedback.cancel(author_id, record.key, expected_etag=revision, **guard)
                    await _edit(message, "Черновик отменён. Отзыв не отправлен; исходное сообщение осталось в чате.")
                    await _answer(query, "Отменено")
                    return
                elif callback_data.action == "n" and callback_data.value in _SELECTIONS:
                    await _answer(query, "Этих данных нет в снимке. Позже они не добавятся автоматически.", alert=True)
                    return
                else:
                    raise FeedbackError("Кнопка не распознана. Открой действующую карточку отзыва.")
                await _refresh(message, record, feedback)
                await _answer(query, "Обновил. Перед отправкой нужен предпросмотр")
            except Conflict:
                try:
                    current = await feedback.get(author_id, callback_data.key, **guard)
                    await _refresh(message, current, feedback)
                except FeedbackError:
                    await _answer(query, "Черновик уже закрыт или устарел. Создай новый через /feedback.", alert=True)
                except FeatureError, RepositoryError, TelegramAPIError, TimeoutError:
                    await _answer(query, "Не удалось обновить карточку. Попробуй ещё раз.", alert=True)
                else:
                    await _answer(query, "Карточка уже изменилась. Обновил кнопки; повтори нужное действие.", alert=True)
            except FeedbackError as error:
                await _answer(query, str(error), alert=True)
            except FeatureError, RepositoryError, TelegramAPIError, TimeoutError:
                await _answer(
                    query, "Не удалось подтвердить действие. Обнови карточку через предпросмотр; не отправляй отзыв повторно.", alert=True
                )

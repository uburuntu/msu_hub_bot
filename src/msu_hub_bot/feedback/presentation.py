"""Literal feedback snapshots and versioned Telegram draft controls."""

from datetime import datetime, timezone

from aiogram.filters.callback_data import CallbackData
from aiogram.methods import SendDocument, SendMessage
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from msu_hub_bot.commands.quiz_view import compact
from msu_hub_bot.storage.features import Record

from .models import FeedbackDraft, FeedbackKind, FeedbackMessage, FeedbackReport

MESSAGE_LIMIT = 4096
KINDS: dict[FeedbackKind, str] = {"bug": "Ошибка", "idea": "Идея", "other": "Другое"}
KIND_VALUES: dict[str, FeedbackKind] = {"b": "bug", "i": "idea", "o": "other"}


def text_size(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _message(message: FeedbackMessage) -> str:
    author = message.author_name or "Автор неизвестен"
    identity = f"; ID {message.author_id}" if message.author_id is not None else ""
    topic = f", тема {message.thread_id}" if message.thread_id is not None else ""
    lines = [f"{author}{identity} · {_time(message.sent_at)}", f"Чат {message.chat_id}{topic}, сообщение {message.message_id}"]
    if message.media_kind:
        lines.append(f"Вложение: {message.media_kind}; сам файл не включён.")
    if message.text:
        lines.append(message.text)
    if message.truncated:
        lines.append("[Сохранён фрагмент сообщения.]")
    if message.chat_id < -1_000_000_000_000:
        lines.append(f"https://t.me/c/{-message.chat_id - 1_000_000_000_000}/{message.message_id}")
    return "\n".join(lines)


def render_report(report: FeedbackReport) -> str:
    """Render only frozen selected content; delivery state never changes the preview."""
    sections = [
        f"Обратная связь · {KINDS[report.kind]}\n\n{report.description}",
        f"Автор: {report.author_name}\nTelegram ID: {report.author_id}\nСоздано: {_time(report.created_at)}",
        "Получатель полного снимка: владелец бота, в закрытом разделе приложения. Хранение — навсегда.\n"
        f"Уведомление с описанием и автором: {report.destination_name}.\nID: {report.report_id}",
    ]
    context = report.context
    if context.origin is not None:
        origin = context.origin
        topic = f"\nТема: {origin.thread_id}" if origin.thread_id is not None else ""
        sections.append(f"Контекст чата\n{origin.label}\nID: {origin.chat_id}{topic}")
    if context.reply is not None:
        sections.append("Сообщение, на которое ответили\n" + _message(context.reply))
    if not context.reply_available:
        sections.append("Сообщение, на которое ответили: контекст недоступен.")
    if context.recent_messages:
        sections.append("Недавние сообщения\n\n" + "\n\n".join(_message(message) for message in context.recent_messages))
    if not context.recent_available:
        sections.append("Недавние сообщения: контекст недоступен.")
    if context.diagnostics_since is not None:
        lines = [f"Диагностика собственных команд с {_time(context.diagnostics_since)}"]
        outcomes = {"completed": "обработчик завершился", "ignored": "пропущено", "cancelled": "отменено", "failed": "ошибка"}
        for entry in context.diagnostics:
            details = [f"{_time(entry.at)} · {entry.handler} · {outcomes[entry.outcome]}"]
            if entry.command:
                details.append(f"Команда: /{entry.command}")
            if entry.message_id is not None:
                details.append(f"Сообщение: {entry.message_id}")
            if entry.reason:
                details.append(f"Категория: {entry.reason}")
            if entry.trace_id:
                details.append(f"Трасса: {entry.trace_id}")
            if entry.release:
                details.append(f"Версия бота: {entry.release}")
            lines.append("\n".join(details))
        lines.append("Завершение обработчика не гарантирует успех внешнего сервиса. Аргументы команд и тексты ошибок не включены.")
        sections.append("\n\n".join(lines))
    return "\n\n".join(sections)


def report_caption(report: FeedbackReport) -> str:
    header = f"Обратная связь · {KINDS[report.kind]}\n\n"
    footer = (
        f"\n\nАвтор: {compact(report.author_name, 128)} (ID {report.author_id})\nID: {report.report_id}\n"
        "Полный снимок — в report.txt; после отправки доступен владельцу бота в приложении. Хранение — навсегда.\n"
        f"Уведомление с описанием и автором: {report.destination_name}."
    )
    summary_budget = min(480, max(0, 1024 - text_size(header + footer)))
    return header + compact(report.description, summary_budget) + footer


def report_method(report: FeedbackReport, chat_id: int) -> SendMessage | SendDocument:
    text = report.rendered_text
    if not text:
        raise ValueError("Feedback report must have a frozen rendering")
    if text_size(text) <= MESSAGE_LIMIT:
        return SendMessage(chat_id=chat_id, text=text, parse_mode=None, link_preview_options=LinkPreviewOptions(is_disabled=True))
    return SendDocument(
        chat_id=chat_id,
        document=BufferedInputFile(text.encode("utf-8"), filename="report.txt"),
        caption=report_caption(report),
        parse_mode=None,
    )


def notification_method(report: FeedbackReport, *, button: InlineKeyboardButton | None = None) -> SendMessage:
    """Notify administrators without forwarding the user's selected context."""
    text = (
        f"📝 {KINDS[report.kind]} · Обратная связь\n\n{compact(report.description, 2000)}\n\n"
        f"Автор: {compact(report.author_name, 128)} (ID {report.author_id})\n"
        f"Создано: {_time(report.created_at)}\nID: {report.report_id}\n"
        "Полный отзыв и выбранный контекст доступны владельцу бота в приложении."
    )
    return SendMessage(
        chat_id=report.destination_chat_id,
        text=text,
        parse_mode=None,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]) if button else None,
    )


class FeedbackCallback(CallbackData, prefix="fb"):
    key: str
    revision: str
    action: str
    value: str


def draft_text(record: Record[FeedbackDraft]) -> str:
    draft = record.value
    notes = []
    if draft.context.reply is None:
        notes.append("Сообщение в ответ: недоступно." if not draft.context.reply_available else "Сообщения в ответ нет.")
    if not draft.context.recent_available:
        notes.append("Недавние сообщения недоступны; позже они не добавятся автоматически.")
    elif not draft.context.recent_messages:
        notes.append("Подходящих недавних сообщений нет.")
    if not draft.context.diagnostics:
        notes.append("Диагностики собственных команд с запуска бота пока нет.")
    else:
        notes.append("Диагностика — только твои команды с запуска бота, без аргументов и текстов ошибок.")
    return (
        f"📝 Обратная связь · {KINDS[draft.kind]}\n\n{compact(draft.description, 400)}\n\n"
        "Полный отзыв увидит владелец бота в закрытом разделе приложения. "
        f"В {draft.destination_name} отправлю уведомление с описанием и автором.\n"
        "Описание и выбранный контекст сохранятся навсегда. "
        f"Твои имя ({draft.author_name}), Telegram ID ({draft.author_id}) и время создания включаются всегда.\n\n"
        "Выбери, что приложить. Перед отправкой покажу точный снимок; сообщения могут быть представлены фрагментами.\n"
        + "\n".join(notes)
        + "\n\nЧерновик доступен 24 часа. Исходное сообщение останется в чате."
    )


def keyboard(record: Record[FeedbackDraft], *, allow_submit: bool = True) -> InlineKeyboardMarkup:
    draft = record.value

    def button(label: str, action: str, value: str = "-") -> InlineKeyboardButton:
        data = FeedbackCallback(key=record.key, revision=record.etag.replace("-", ""), action=action, value=value).pack()
        return InlineKeyboardButton(text=label, callback_data=data)

    rows = [[button(("✓ " if draft.kind == kind else "") + KINDS[kind], "k", value) for value, kind in KIND_VALUES.items()]]
    for field, action, label, available in (
        ("chat", "c", "Чат и тема", True),
        ("reply", "r", "Сообщение в ответ", draft.context.reply is not None),
        ("recent", "h", "Недавние сообщения", bool(draft.context.recent_messages) and draft.context.recent_available),
        ("diagnostics", "d", "Свои команды: диагностика", bool(draft.context.diagnostics)),
    ):
        selected = getattr(draft.selection, field)
        rows.append(
            [button(("☑ " if selected else "☐ ") + label, action, "0" if selected else "1")]
            if available
            else [button("— " + label + ": нет данных", "n", action)]
        )
    if allow_submit and draft.preview_digest is not None:
        rows.append([button("✅ Отправить отзыв", "s")])
    rows.append([button("Предпросмотр", "p"), button("Отменить", "x")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

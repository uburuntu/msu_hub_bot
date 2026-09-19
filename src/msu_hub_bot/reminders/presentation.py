"""Shared reminder acknowledgements for Telegram and authenticated web creation."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from msu_hub_bot.commands.quiz_view import compact
from msu_hub_bot.storage.features import Record

from .models import Reminder
from .service import when

STATUSES = {
    "pending": "ждёт",
    "sending": "отправляется",
    "delivered": "доставлено",
    "cancelled": "отменено",
    "failed": "ошибка доставки",
    "uncertain": "доставка не подтверждена",
}


class ReminderCallback(CallbackData, prefix="remind"):
    key: str
    revision: str
    action: str


def keyboard(record: Record[Reminder]) -> InlineKeyboardMarkup | None:
    if record.value.status != "pending":
        return None

    def button(label: str, action: str) -> InlineKeyboardButton:
        data = ReminderCallback(key=record.key, revision=record.etag.replace("-", ""), action=action).pack()
        return InlineKeyboardButton(text=label, callback_data=data)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("+10 минут", "10m"), button("+1 час", "1h"), button("+24 часа", "1d")],
            [button("Отменить", "off")],
        ]
    )


def confirmation(record: Record[Reminder]) -> str:
    repeat = ""
    if rule := record.value.recurrence:
        label = {"daily": "каждый день", "weekly": "каждую неделю", "interval": f"каждые {rule.interval_minutes} мин."}[rule.kind]
        repeat = f"\n🔁 {label}; отмена останавливает всю серию"
    return f"⏰ {when(record.value)} — {STATUSES[record.value.status]}{repeat}\n{compact(record.value.text, 160)}\n\nID: {record.key}"

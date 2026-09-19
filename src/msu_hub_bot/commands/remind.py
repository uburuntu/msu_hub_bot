"""Telegram entry points for personal reminders in the current chat and topic."""

import asyncio
from datetime import timedelta
from uuid import UUID

from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from msu_hub_bot.commands.quiz_view import compact
from msu_hub_bot.reminders import ReminderError, ReminderService, Schedule, parse_schedule
from msu_hub_bot.reminders.presentation import ReminderCallback, confirmation, keyboard
from msu_hub_bot.storage.features import Conflict, FeatureError
from msu_hub_bot.storage.supabase import RepositoryError
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.web.links import WebAppLinks

HELP = (
    "⏰ Напомню здесь, в этой же теме. Часовой пояс — из /app, по умолчанию московский. Повторения настраиваются в /app.\n\n"
    "/remind in 15m выключить духовку\n"
    "/remind через 2ч 30м размяться\n"
    "/remind at 2027-04-15 09:30 [Europe/Moscow] встреча\n\n"
    "/remind list — твои напоминания здесь\n"
    "/remind edit ID in 1h — перенести\n"
    "/remind cancel ID — отменить\n"
    "/remind retry ID — повторить после ошибки. Если доставка не подтверждена, возможен повтор сообщения."
)


async def answer(query: CallbackQuery, text: str, *, alert: bool = False) -> None:
    async with asyncio.timeout(15):
        await bot_for(query)(query.answer(text, show_alert=alert), request_timeout=15)


class Remind:
    callback_data = ReminderCallback

    @staticmethod
    async def process(message: Message, meta: MetaInfo, reminders: ReminderService, web_apps: WebAppLinks | None = None) -> Message:
        markup = None
        try:
            if message.from_user is None or message.from_user.is_bot or message.sender_chat is not None:
                raise ReminderError("Создай напоминание от личного аккаунта: тогда только ты сможешь его менять.")
            user = message.from_user
            text = meta.text.strip()
            if not text or text.casefold() in {"help", "помощь"}:
                body = HELP
            else:
                parts = text.split(maxsplit=2)
                action = parts[0].casefold()
                if action in {"list", "список"}:
                    after = parts[1] if len(parts) > 1 else None
                    rows = await reminders.list(
                        user.id, chat_id=message.chat.id, thread_id=message.message_thread_id, after=after, limit=10
                    )
                    body = (
                        "Твои напоминания здесь:\n\n" + "\n\n".join(confirmation(row) for row in rows)
                        if rows
                        else "Здесь пока нет твоих напоминаний.\n/remind in 15m чай"
                    )
                    if len(rows) == 10:
                        body += f"\n\nДальше: /remind list {rows[-1].key}"
                    if any(row.value.status == "uncertain" for row in rows):
                        body += "\n\nДоставка не подтверждена: /remind retry ID отправит снова; возможен повтор."
                elif action in {"cancel", "edit", "retry", "отмена", "перенести"}:
                    if len(parts) < 2:
                        raise ReminderError("Добавь ID из /remind list.")
                    key = parts[1]
                    if action in {"cancel", "отмена"}:
                        record = await reminders.cancel(user.id, key, chat_id=message.chat.id, thread_id=message.message_thread_id)
                    elif action == "retry":
                        record = await reminders.retry(user.id, key, chat_id=message.chat.id, thread_id=message.message_thread_id)
                    else:
                        if len(parts) < 3:
                            raise ReminderError("Укажи новое время: /remind edit ID in 1h.")
                        previous = await reminders.get(user.id, key, chat_id=message.chat.id, thread_id=message.message_thread_id)
                        schedule = parse_schedule(parts[2], reminders.clock(), previous.value.timezone)
                        record = await reminders.reschedule(
                            user.id,
                            key,
                            schedule,
                            expected_etag=previous.etag,
                            chat_id=message.chat.id,
                            thread_id=message.message_thread_id,
                        )
                    body, markup = confirmation(record), keyboard(record)
                else:
                    schedule = parse_schedule(text, reminders.clock(), await reminders.preferences.timezone(user.id))
                    record = await reminders.create(
                        author_id=user.id,
                        author_name=compact(user.full_name, 256),
                        chat_id=message.chat.id,
                        thread_id=message.message_thread_id,
                        source_message_id=message.message_id,
                        schedule=schedule,
                    )
                    body, markup = confirmation(record), keyboard(record)
        except ReminderError as error:
            body = str(error)
        except Conflict, FeatureError, RepositoryError, TimeoutError, ValueError, TelegramAPIError:
            body = "Не удалось подтвердить изменение. Проверь /remind list перед повтором."
        if web_apps is not None and (button := web_apps.button(message, now=reminders.clock())) is not None:
            markup = InlineKeyboardMarkup(inline_keyboard=[*(markup.inline_keyboard if markup else []), [button]])
        async with asyncio.timeout(15):
            return await bot_for(message)(message.reply(body, parse_mode=None, reply_markup=markup), request_timeout=15)

    @staticmethod
    async def process_cb(query: CallbackQuery, callback_data: ReminderCallback, reminders: ReminderService) -> None:
        if not isinstance(query.message, Message):
            await answer(query, "Это напоминание недоступно.", alert=True)
            return
        message = query.message
        try:
            revision = str(UUID(hex=callback_data.revision))
            record = await reminders.get(
                query.from_user.id, callback_data.key, chat_id=message.chat.id, thread_id=message.message_thread_id
            )
            if callback_data.action == "off":
                record = await reminders.cancel(query.from_user.id, record.key, expected_etag=revision)
            else:
                seconds = {"10m": 600, "1h": 3600, "1d": 86400}.get(callback_data.action)
                if seconds is None:
                    raise ReminderError("Неизвестная кнопка.")
                schedule = Schedule(due_at=record.value.due_at + timedelta(seconds=seconds), timezone=record.value.timezone)
                record = await reminders.reschedule(query.from_user.id, record.key, schedule, expected_etag=revision)
            await answer(query, "Готово")
            async with asyncio.timeout(15):
                await bot_for(message)(
                    message.edit_text(confirmation(record), parse_mode=None, reply_markup=keyboard(record)), request_timeout=15
                )
        except ReminderError as error:
            await answer(query, str(error), alert=True)
        except Conflict, FeatureError, RepositoryError, TimeoutError, ValueError, TelegramAPIError:
            await answer(query, "Напоминание изменилось. Открой /remind list.", alert=True)

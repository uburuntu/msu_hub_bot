"""Mini App entry points preserve the originating user, chat and forum topic."""

from datetime import UTC, datetime

from aiogram.filters import CommandObject
from aiogram.types import InlineKeyboardMarkup, Message

from msu_hub_bot.web.links import WebAppLinks


def is_app_start(message: Message, command: CommandObject) -> bool:
    return message.chat.type == "private" and bool(command.args and command.args.startswith("app_"))


async def process_app(message: Message, web_apps: WebAppLinks) -> Message:
    if message.sender_chat is not None or message.from_user is None or message.from_user.is_bot:
        return await message.reply("Открой /app от личного аккаунта: напоминания будут доступны только тебе.")
    button = web_apps.button(message, now=datetime.now(UTC))
    if button is None:
        return await message.reply("Приложение пока недоступно. Напоминания работают через /remind.")
    return await message.reply(
        "Твои напоминания — в одном месте. Новые будут приходить в этот чат и тему.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )


async def process_app_start(message: Message, command: CommandObject, web_apps: WebAppLinks) -> Message:
    if not web_apps.url or message.from_user is None:
        return await process_app(message, web_apps)
    launch = (command.args or "")[4:]
    try:
        web_apps.destination(message.from_user.id, launch, now=datetime.now(UTC))
    except ValueError:
        return await message.reply("Ссылка устарела или принадлежит другому человеку. Вызови /app в нужном чате.")
    return await message.reply(
        "Открой приложение — я запомнил, из какого чата ты пришёл.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[web_apps.private_button(launch)]]),
    )

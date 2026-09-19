"""One durable raffle card: unique entrants, paginated names and a fixed winner."""

import asyncio
import logging
from datetime import timedelta
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters, User
from aiogram.utils.formatting import Bold, Text
from pydantic import Field

from msu_hub_bot.commands.quiz_view import compact, user_label
from msu_hub_bot.games.raffle import PAGE_SIZE, Person, RaffleError, RaffleRound, RaffleStore
from msu_hub_bot.storage.features import Record
from msu_hub_bot.telegram.context import bot_for

logger = logging.getLogger(__name__)


class RaffleCallback(CallbackData, prefix="raffle"):
    action: Literal["reg", "winner", "page", "refresh"]
    token: str = Field(pattern=r"^[a-f0-9]{16}$")
    page: int = Field(default=0, ge=0, le=2**31 - 1)


def person(user: User) -> Person:
    return Person(user_id=user.id, name=compact(user.full_name, 256) or "Участник", username=user.username)


def thread(message: Message) -> int | None:
    return message.message_thread_id if message.is_topic_message else None


def label(user: Person) -> Text:
    return user_label(user.user_id, user.name, user.username)


def keyboard(value: RaffleRound, page: int = 0, pages: int = 1) -> InlineKeyboardMarkup:
    def button(text: str, action: Literal["reg", "winner", "page", "refresh"], number: int = page) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=RaffleCallback(action=action, token=value.token, page=number).pack())

    rows: list[list[InlineKeyboardButton]] = []
    if value.status == "open":
        rows.extend([[button("Участвую! 🎈", "reg")], [button("Выбрать победителя 👑", "winner")]])
    if pages > 1:
        navigation = []
        if page > 0:
            navigation.append(button("‹ Назад", "page", page - 1))
        navigation.append(button(f"{page + 1}/{pages}", "page"))
        if page + 1 < pages:
            navigation.append(button("Дальше ›", "page", page + 1))
        rows.append(navigation)
    rows.append([button("Обновить результат" if value.status == "finished" else "Обновить участников", "refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def view(value: RaffleRound, entries: list[Person], page: int, pages: int) -> Text:
    rows: list[Text | str] = ["✨ ", Bold("Конкурс"), " от ", label(value.creator)]
    if value.winner is not None:
        rows.extend(["\n\n", Bold("👑 Победитель: "), label(value.winner), "\nШарики взлетели. Поздравляем! 🎉"])
    rows.append(f"\n\nУчастников: {value.participants}\n")
    rows.extend(Text(f"\n{number}. ", label(entry)) for number, entry in enumerate(entries, page * PAGE_SIZE + 1))
    if not value.participants:
        rows.append("Пока никого. Первый шарик твой? 🎈")
    if pages > 1:
        rows.append(f"\n\nСтраница {page + 1}/{pages}. Все участники — в розыгрыше.")
    if value.status == "open":
        rows.append("\n\nОдин человек — один шарик. Победителя выбирает создатель конкурса.")
    return Text(*rows)


def valid_origin(row: Record[RaffleRound], message: Message) -> bool:
    value = row.value
    if (
        message.chat.id != value.chat_id
        or thread(message) != value.thread_id
        or message.from_user is None
        or message.from_user.id != value.bot_id
        or not message.from_user.is_bot
        or message.forward_origin is not None
    ):
        return False
    if value.message_id is not None:
        return message.message_id == value.message_id
    return (
        value.created_at - timedelta(seconds=1) <= message.date <= value.created_at + timedelta(minutes=2)
        and message.reply_to_message is not None
        and message.reply_to_message.message_id == value.reply_message_id
        and message.reply_markup is not None
        and message.reply_markup.model_dump(mode="json") == keyboard(value).model_dump(mode="json")
    )


async def render(bot: Bot, raffles: RaffleStore, row: Record[RaffleRound], page: int) -> None:
    async with asyncio.timeout(15), raffles.render_lock(row.key):
        current = await raffles.require(row.scope, row.key)
        value = current.value
        if value.message_id is None:
            return
        entries, page, pages = await raffles.page(current, page)
        try:
            await bot.edit_message_text(
                chat_id=value.chat_id,
                message_id=value.message_id,
                **view(value, entries, page, pages).as_kwargs(),
                reply_markup=keyboard(value, page, pages),
                request_timeout=12,
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in error.message.lower():
                raise


class Raffle:
    callback_data = RaffleCallback

    @staticmethod
    async def process(message: Message, raffles: RaffleStore) -> Message | None:
        if message.from_user is None or message.sender_chat is not None or message.from_user.is_bot:
            return await message.reply("Для розыгрыша нужна команда от тебя лично — так только ты сможешь выбрать победителя.")
        bot = bot_for(message)
        try:
            async with asyncio.timeout(15):
                row, created = await raffles.create(
                    message.chat.id,
                    thread(message),
                    message.message_id,
                    message.reply_to_message.message_id if message.reply_to_message else message.message_id,
                    person(message.from_user),
                )
            if not created:
                if row.value.message_id is not None:
                    await render(bot, raffles, row, 0)
                    return None
                return await message.reply(
                    "Карточку не удалось подтвердить. Если она появилась, нажми на ней «Обновить участников». "
                    "Если нет — отправь /raffle новой командой."
                )
            value = row.value
            async with asyncio.timeout(15):
                sent = await bot.send_message(
                    value.chat_id,
                    message_thread_id=value.thread_id,
                    reply_parameters=ReplyParameters(message_id=value.reply_message_id),
                    **view(value, [], 0, 1).as_kwargs(),
                    reply_markup=keyboard(value),
                    request_timeout=12,
                )
            try:
                async with asyncio.timeout(10):
                    await raffles.bind(row.scope, row.key, sent.message_id)
            except Exception:
                logger.exception("Raffle publication binding unavailable")
            return sent
        except Exception:
            logger.exception("Raffle creation unavailable")
            async with asyncio.timeout(15):
                return await bot(
                    message.reply(
                        "Не удалось подтвердить розыгрыш. Если карточка появилась — нажми её кнопку обновления. "
                        "Иначе попробуй новую команду чуть позже."
                    ),
                    request_timeout=12,
                )

    @staticmethod
    async def process_cb(query: CallbackQuery, callback_data: RaffleCallback, raffles: RaffleStore) -> None:
        message = query.message
        answer = ""
        row: Record[RaffleRound] | None = None
        authorized = False
        try:
            if not isinstance(message, Message) or query.from_user.is_bot:
                raise RaffleError("Эта кнопка уже недоступна. Начни новый /raffle.")
            async with asyncio.timeout(10):
                scope = raffles.scope(message.chat.id, thread(message))
                row = await raffles.require(scope, callback_data.token)
                if not valid_origin(row, message):
                    raise RaffleError("Эта кнопка относится к другому сообщению.")
                authorized = True
                if row.value.message_id is None:
                    row = await raffles.bind(scope, row.key, message.message_id)
                if callback_data.action == "reg":
                    row, joined = await raffles.join(scope, row.key, person(query.from_user))
                    answer = "🎈 Ты участвуешь!" if joined else "🎈 Твой шарик уже здесь. Второй не понадобится."
                elif callback_data.action == "winner":
                    row = await raffles.draw(scope, row.key, query.from_user.id)
                    answer = "👑 Победитель выбран!"
        except RaffleError as error:
            answer = str(error)
        except Exception:
            logger.exception("Raffle action unavailable")
            answer = "Не удалось проверить розыгрыш. Нажми ещё раз — участие не потеряется."
        try:
            async with asyncio.timeout(10):
                await bot_for(query)(query.answer(answer), request_timeout=8)
        except Exception:
            logger.warning("Raffle callback acknowledgement unavailable")
        if authorized and row is not None:
            try:
                await render(bot_for(query), raffles, row, callback_data.page)
            except Exception:
                logger.exception("Raffle card refresh unavailable")

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, ParamSpec, TypeVar, cast
from contextlib import suppress
from enum import Enum

from aiogram.exceptions import TelegramBadRequest
from aiocache import cached
from aiogram import Bot
from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import ChatFullInfo, Message, TelegramObject, InlineKeyboardMarkup
from aiogram.utils.markdown import hbold, hitalic, hlink, hcode, hide_link

from common.db.edb import EdgeDB
from hub_bot.db import EcosystemChat
from common.tg.utils import chat_url, chat_link, sender_mention
from common.utils import random_cycle


P = ParamSpec("P")
T = TypeVar("T")


def _cached(function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    return cast(Callable[P, Awaitable[T]], cached(ttl=600, noself=True)(function))


class EventsMiddleware(BaseMiddleware):
    def __init__(self, bot: Bot, db: EdgeDB, events_chat_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.db = db
        self.events_chat_id = events_chat_id
        self.em = EcosystemManager(bot, db)

    async def send_event(self, text: str, preview: bool = False, keyboard: InlineKeyboardMarkup | None = None) -> Message | None:
        if not self.events_chat_id:
            return None
        return await self.bot.send_message(self.events_chat_id, text, disable_web_page_preview=not preview, reply_markup=keyboard)

    async def log_event_from_ic(self, message: Message) -> Message | None:
        if not await EcosystemChat.query(self.db).exist_cached(message.chat.id):
            return None

        chat = message.chat
        link = await chat_link(chat, force_link=True)

        if message.new_chat_members:
            text = f"👤 Новый пользователь " if len(message.new_chat_members) == 1 else f"👥 Новые пользователи "
            text += f"в {link}:\n"
            for member in message.new_chat_members:
                text += f"— {member.mention_html()}, #{member.id}\n"

            event = await self.send_event(text)
            await self.em.update_pins()
            return event

        if message.left_chat_member:
            text = f"👣 Ушел пользователь из {link}:\n— {message.left_chat_member.mention_html()}, #{message.left_chat_member.id}"

            event = await self.send_event(text)
            await self.em.update_pins()
            return event

        if message.new_chat_title:
            text = f"🔄 Новое название чата: {link}"

            return await self.send_event(text)

        if message.new_chat_photo:
            text = f"🔄 Новое фото у чата: {link}"

            return await self.send_event(text)

        if message.delete_chat_photo:
            text = f"🔄 Удалено фото чата: {link}"

            return await self.send_event(text)

        if message.pinned_message:
            pinned = message.pinned_message
            url = pinned.get_url() if isinstance(pinned, Message) else None
            label = str(pinned.message_id)
            text = f"📍 Новый закреп в {link}: {hlink(label, url) if url else hcode(label)}"
            return await self.send_event(text)

        return None

    async def log_event(self, message: Message) -> None:
        if message.new_chat_members:
            for member in message.new_chat_members:
                if member.id == self.bot.id:
                    user_link = sender_mention(message)
                    link = await chat_link(message.chat, force_link=True)
                    members = await self.bot.get_chat_member_count(message.chat.id)
                    text = f"❇️ {user_link} добавил бота в чат:\n— {link}, {members} уч."
                    await self.send_event(text)
                    break

        if message.group_chat_created or message.supergroup_chat_created or message.channel_chat_created:
            user_link = sender_mention(message)
            link = await chat_link(message.chat, force_link=True)
            members = await self.bot.get_chat_member_count(message.chat.id)
            chat_type = "канал" if message.chat.type == ChatType.CHANNEL else "чат"
            text = f"❇️ {user_link} создал {chat_type} с ботом:\n— {link}, {members} уч."
            await self.send_event(text)

        if message.migrate_from_chat_id:
            await message.answer(
                f"✳️ Этот чат мигрировал в супергруппу, предыдущий номер: {hcode(message.migrate_from_chat_id)}, "
                f"текущий номер: {hcode(message.chat.id)}.\n\n"
                f"{hitalic('Примечание')}: реплаи на сообщения сверху работать не будут.\n\n"
                f"{hitalic('Примечание')}: если вы создавали стикеры чата, то сейчас они мне будут недоступны. "
                f"Но это не помешает мне создать новый пак."
            )

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        if isinstance(event, Message):
            await self.log_event(event)
            await self.log_event_from_ic(event)
        return await handler(event, data)


class EcosystemManager:
    def __init__(self, bot: Bot, db: EdgeDB) -> None:
        self.bot = bot
        self.db = db

        self.images = cast(Callable[..., Iterator[str]], random_cycle)(
            "https://i.imgur.com/AA3fgbf.png",
            "https://i.imgur.com/1bi3Aki.png",
            "https://i.imgur.com/PRZpGY0.png",
            "https://i.imgur.com/RKdrRVo.jpeg",
            "https://i.imgur.com/Cuem492.jpeg",
            "https://i.imgur.com/vuCnUnI.jpeg",
            "https://i.imgur.com/VjkoGa3.jpeg",
            "https://i.imgur.com/1TKmsDQ.jpeg",
            "https://i.imgur.com/8i3wjV4.jpeg",
            "https://i.imgur.com/ygG7bhD.jpeg",
        )

    async def pin(self, chat_id: int, forced: bool = False) -> Message | bool:
        e_chat = await EcosystemChat.query(self.db).get_cached(chat_id)

        if e_chat and e_chat.pinned_message_id:
            if not forced:
                return True

            with suppress(TelegramBadRequest):
                await self.bot.delete_message(chat_id, e_chat.pinned_message_id)

        text = await self.text(chat_id)
        pin_msg = await self.bot.send_message(chat_id, text)
        await pin_msg.pin(disable_notification=True)
        await EcosystemChat.query(self.db).update(pk=chat_id, pinned_message_id=pin_msg.message_id)
        return pin_msg

    @_cached
    async def update_pins(self) -> None:
        await self.update_ic_members()

        e_chats = await EcosystemChat.query(self.db).get_all_cached()
        coros = [
            self.bot.edit_message_text(await self.text(ec.chat_id), ec.chat_id, ec.pinned_message_id)
            for ec in e_chats.values()
            if ec.pinned_message_id
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, TelegramBadRequest):
                raise result

    async def update_ic_members(self) -> list[EcosystemChat | None]:
        e_chats = await EcosystemChat.query(self.db).get_all_cached()

        async def update_single(ec: EcosystemChat) -> EcosystemChat | None:
            chat = await self.get_chat(ec.chat_id)
            members = await self.bot.get_chat_member_count(chat.id)
            if members != ec.members:
                return cast(EcosystemChat, await EcosystemChat.query(self.db).update(pk=ec.chat_id, members=members))
            return None

        results = await asyncio.gather(*(update_single(ec) for ec in e_chats.values()), return_exceptions=True)
        saved: list[EcosystemChat | None] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            saved.append(result)
        return saved

    @_cached
    async def get_chat(self, chat_id: int) -> ChatFullInfo:
        return await self.bot.get_chat(chat_id)

    @_cached
    async def link(self, chat_id: int) -> str:
        chat = await self.get_chat(chat_id)
        if chat.username:
            return "@" + chat.username

        e_chats = await EcosystemChat.query(self.db).get_all_cached()
        if alias := e_chats[chat_id].username_alias:
            return "@" + str(alias)

        url = await chat_url(chat, force_link=True)
        return hlink("ссылка", url) if url else "ссылка"

    class ChatGroup(Enum):
        main = "⚜️ Основные ресурсы"
        dormitory = "🏘 По общежитиям"
        faculty = "👨🏻‍🎓 По факультетам"
        filial = "🗺 По филиалам"
        interest = "🗿 Тематика"
        camp = "🏖 Лагеря"
        other = "🍒 Всякие разные"
        channel = "📜 Каналы"

    async def text(self, chat_id: int | None = None) -> str:
        e_chats = await EcosystemChat.query(self.db).get_all_cached()

        groups: defaultdict[str, list[EcosystemChat]] = defaultdict(list)
        for e_chat in e_chats.values():
            groups[e_chat.section].append(e_chat)

        text = hide_link(next(self.images)) + hbold("Экосистема чатов МГУ ✨\n")
        text += (
            "— это сообщество студентов и выпускников МГУ\n"
            "— здесь приветствуется взаимопомощь в любом виде\n"
            "— спам удаляется, а агрессия не одобряется\n\n"
        )

        if chat_id is not None and (e_chats[chat_id].members or 0) < 30:
            text += (
                hitalic("Disclaimer: ") + "этот чат развивается, поэтому здесь еще мало людей, "
                "но если приглашать друзей и вести интересные обсуждения, "
                "то совсем скоро он оживет.\n\n"
            )

        for group in self.ChatGroup:
            chats = sorted(filter(lambda ec: not ec.is_hidden, groups[group.name]), key=lambda x: x.members or 0, reverse=True)
            if chats:
                text += f"{hbold(group.value)}:\n"
                for chat in chats:
                    text += "— {name}{members} | {link}\n".format(
                        name=chat.name,
                        members=" | " + hitalic(f"{chat.members} уч.") if chat.members else "",
                        link=await self.link(chat.chat_id),
                    )
                text += "\n"

        text += hbold("Приятного общения ✌🏻")

        return text

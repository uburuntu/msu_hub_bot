from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast
from time import monotonic
from contextlib import suppress
from enum import Enum

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram import Bot
from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import ChatFullInfo, Message, TelegramObject, InlineKeyboardMarkup
from aiogram.utils.markdown import hbold, hitalic, hlink, hcode, hide_link
from cachetools import TTLCache

from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.models import DirectoryPatch, DirectoryRecord
from msu_hub_bot.telegram.utils import chat_url, chat_link, sender_mention
from msu_hub_bot.utils import random_cycle


def _chat_unavailable(error: TelegramBadRequest | TelegramForbiddenError) -> bool:
    if isinstance(error, TelegramBadRequest):
        return error.message.casefold().removeprefix("bad request: ") in {"chat not found", "private chat not found"}
    return error.message.casefold().removeprefix("forbidden: ") in {
        "the group chat was deleted",
        "bot was kicked from the group chat",
        "bot was kicked from the supergroup chat",
        "bot was kicked from the channel chat",
        "bot is not a member of the group chat",
        "bot is not a member of the supergroup chat",
        "bot is not a member of the channel chat",
    }


class EventsMiddleware(BaseMiddleware):
    def __init__(self, bot: Bot, db: BotRepository, events_chat_id: int, *, em: EcosystemManager | None = None) -> None:
        super().__init__()
        self.bot = bot
        self.db = db
        self.events_chat_id = events_chat_id
        self.em = em or EcosystemManager(bot, db)

    async def send_event(self, text: str, preview: bool = False, keyboard: InlineKeyboardMarkup | None = None) -> Message | None:
        if not self.events_chat_id:
            return None
        return await self.bot.send_message(self.events_chat_id, text, disable_web_page_preview=not preview, reply_markup=keyboard)

    async def log_event_from_ic(self, message: Message) -> Message | None:
        if message.chat.id not in await self.em.directory():
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
    def __init__(self, bot: Bot, db: BotRepository) -> None:
        self.bot = bot
        self.db = db
        self._directory: dict[int, DirectoryRecord] = {}
        self._directory_until = 0.0
        self._directory_lock = asyncio.Lock()
        self._chats: TTLCache[int, ChatFullInfo | None] = TTLCache(maxsize=1024, ttl=600, timer=monotonic)
        self._pins_until = 0.0
        self._pins_lock = asyncio.Lock()

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

    def invalidate_directory(self) -> None:
        self._directory_until = 0.0

    async def directory(self, *, refresh: bool = False) -> dict[int, DirectoryRecord]:
        async with self._directory_lock:
            if refresh or monotonic() >= self._directory_until:
                self._directory = {chat.chat_id: chat for chat in await self.db.list_directory()}
                self._directory_until = monotonic() + 300
            return dict(self._directory)

    async def pin(self, chat_id: int, forced: bool = False) -> Message | bool:
        e_chat = (await self.directory()).get(chat_id)
        if forced:
            self._chats.pop(chat_id, None)
        if await self.get_chat(chat_id) is None:
            return False

        if e_chat and e_chat.pinned_message_id:
            if not forced:
                return True

            with suppress(TelegramBadRequest):
                await self.bot.delete_message(chat_id, e_chat.pinned_message_id)

        text = await self.text(chat_id)
        pin_msg = await self.bot.send_message(chat_id, text)
        await pin_msg.pin(disable_notification=True)
        await self.db.patch_directory(chat_id, DirectoryPatch(pinned_message_id=pin_msg.message_id))
        self.invalidate_directory()
        return pin_msg

    async def update_pins(self, *, forced: bool = False) -> None:
        async with self._pins_lock:
            if not forced and monotonic() < self._pins_until:
                return
            if forced:
                self._chats.clear()
            await self._update_pins()
            self._pins_until = monotonic() + 600

    async def _update_pins(self) -> None:
        await self.update_ic_members()

        e_chats = await self.directory(refresh=True)

        async def update_pin(ec: DirectoryRecord) -> None:
            if not ec.pinned_message_id or await self.get_chat(ec.chat_id) is None:
                return
            text = await self.text(ec.chat_id)
            try:
                await self.bot.edit_message_text(text, chat_id=ec.chat_id, message_id=ec.pinned_message_id)
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                if _chat_unavailable(error):
                    self._chats[ec.chat_id] = None
                    return
                if isinstance(error, TelegramBadRequest):
                    description = error.message.casefold().removeprefix("bad request: ")
                    if description in {
                        "message is not modified",
                        "message to edit not found",
                        "message can't be edited",
                    } or description.startswith("message is not modified: "):
                        return
                raise

        results = await asyncio.gather(*(update_pin(ec) for ec in e_chats.values()), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def update_ic_members(self) -> list[DirectoryRecord | None]:
        e_chats = await self.directory(refresh=True)

        async def update_single(ec: DirectoryRecord) -> DirectoryRecord | None:
            chat = await self.get_chat(ec.chat_id)
            if chat is None:
                return None
            try:
                members = await self.bot.get_chat_member_count(chat.id)
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                if not _chat_unavailable(error):
                    raise
                self._chats[ec.chat_id] = None
                return None
            if members != ec.members:
                return await self.db.patch_directory(ec.chat_id, DirectoryPatch(members=members))
            return None

        results = await asyncio.gather(*(update_single(ec) for ec in e_chats.values()), return_exceptions=True)
        self.invalidate_directory()
        saved: list[DirectoryRecord | None] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            saved.append(result)
        return saved

    async def get_chat(self, chat_id: int) -> ChatFullInfo | None:
        try:
            return self._chats[chat_id]
        except KeyError:
            pass
        try:
            chat = await self.bot.get_chat(chat_id)
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            if not _chat_unavailable(error):
                raise
            chat = None
        # Access can return later; never remove directory data for a failed lookup.
        self._chats[chat_id] = chat
        return chat

    async def link(self, chat_id: int) -> str:
        chat = await self.get_chat(chat_id)
        if chat is not None and chat.username:
            return "@" + chat.username

        entry = (await self.directory()).get(chat_id)
        if entry is not None and (alias := entry.username_alias):
            return "@" + str(alias)

        if chat is None:
            return "временно недоступен"
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
        e_chats = await self.directory()

        groups: defaultdict[str, list[DirectoryRecord]] = defaultdict(list)
        for e_chat in e_chats.values():
            groups[e_chat.section].append(e_chat)

        text = hide_link(next(self.images)) + hbold("Экосистема чатов МГУ ✨\n")
        text += (
            "— это сообщество студентов и выпускников МГУ\n"
            "— здесь приветствуется взаимопомощь в любом виде\n"
            "— спам удаляется, а агрессия не одобряется\n\n"
        )

        if chat_id is not None and (entry := e_chats.get(chat_id)) is not None and (entry.members or 0) < 30:
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

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast
from time import monotonic
from contextlib import suppress
from enum import Enum

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram import Bot
from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Chat, ChatFullInfo, ChatMemberUpdated, Message, TelegramObject, InlineKeyboardMarkup
from aiogram.utils.markdown import hbold, hitalic, hlink, hcode, hide_link
from cachetools import TTLCache

from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.models import DirectoryPatch, DirectoryRecord
from msu_hub_bot.telemetry import Boundary, Outcome, Provider, Telemetry
from msu_hub_bot.telegram.errors import telegram_error_reason
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
        return await self.em.request(
            lambda: self.bot.send_message(self.events_chat_id, text, disable_web_page_preview=not preview, reply_markup=keyboard)
        )

    async def log_event_from_ic(self, message: Message) -> Message | None:
        membership_changed = bool(message.new_chat_members or message.left_chat_member)
        if not membership_changed and (
            not self.events_chat_id
            or not (message.new_chat_title or message.new_chat_photo or message.delete_chat_photo or message.pinned_message)
        ):
            return None
        if message.chat.id not in await self.em.directory():
            return None

        if not self.events_chat_id:
            await self.em.update_pins()
            return None
        chat = message.chat
        link = await self.em.chat_link(chat)

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
        if self.events_chat_id and message.new_chat_members:
            for member in message.new_chat_members:
                if member.id == self.bot.id:
                    user_link = sender_mention(message)
                    link = await self.em.chat_link(message.chat)
                    members = await self.em.request(lambda: self.bot.get_chat_member_count(message.chat.id))
                    if members is None:
                        break
                    text = f"❇️ {user_link} добавил бота в чат:\n— {link}, {members} уч."
                    await self.send_event(text)
                    break

        if self.events_chat_id and (message.group_chat_created or message.supergroup_chat_created or message.channel_chat_created):
            user_link = sender_mention(message)
            link = await self.em.chat_link(message.chat)
            members = await self.em.request(lambda: self.bot.get_chat_member_count(message.chat.id))
            if members is not None:
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
        if isinstance(event, ChatMemberUpdated) and event.new_chat_member.user.id == self.bot.id:
            self.em.invalidate_chat(event.chat.id)
        elif isinstance(event, Message) and any(
            (
                event.new_chat_members,
                event.left_chat_member,
                event.new_chat_title,
                event.new_chat_photo,
                event.delete_chat_photo,
                event.pinned_message,
                event.group_chat_created,
                event.supergroup_chat_created,
                event.channel_chat_created,
                event.migrate_from_chat_id,
            )
        ):
            with self.em.telemetry.operation(Boundary.PROVIDER, "ecosystem.events", provider=Provider.TELEGRAM) as observation:
                await self.log_event(event)
                await self.log_event_from_ic(event)
                if self.em.throttled:
                    observation.set_outcome(Outcome.UNAVAILABLE)
        return await handler(event, data)


class EcosystemManager:
    def __init__(self, bot: Bot, db: BotRepository, *, telemetry: Telemetry | None = None) -> None:
        self.bot = bot
        self.db = db
        self.telemetry = telemetry or Telemetry()
        self._directory: dict[int, DirectoryRecord] = {}
        self._directory_until = 0.0
        self._directory_lock = asyncio.Lock()
        self._chats: TTLCache[int, ChatFullInfo | None] = TTLCache(maxsize=1024, ttl=600, timer=monotonic)
        self._chat_lock = asyncio.Lock()
        self._requests = asyncio.Semaphore(3)
        self._retry_until = 0.0
        self._uneditable_pins: TTLCache[int, int] = TTLCache(maxsize=1024, ttl=3600, timer=monotonic)
        self._chat_epoch = 0
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

    def invalidate_chat(self, chat_id: int) -> None:
        self._chats.pop(chat_id, None)
        self._uneditable_pins.pop(chat_id, None)
        self._chat_epoch += 1
        self._pins_until = 0.0

    @property
    def throttled(self) -> bool:
        return monotonic() < self._retry_until

    async def request[T](self, make_request: Callable[[], Awaitable[T]]) -> T | None:
        """Defer optional maintenance without making the next update wait for Telegram."""
        if self.throttled:
            return None
        async with self._requests:
            if self.throttled:
                return None
            try:
                return await make_request()
            except TelegramRetryAfter as error:
                self._retry_until = max(self._retry_until, monotonic() + max(1, error.retry_after))
                return None

    async def directory(self, *, refresh: bool = False) -> dict[int, DirectoryRecord]:
        async with self._directory_lock:
            if refresh or monotonic() >= self._directory_until:
                self._directory = {chat.chat_id: chat for chat in await self.db.list_directory()}
                self._directory_until = monotonic() + 300
            return dict(self._directory)

    async def pin(self, chat_id: int, forced: bool = False) -> Message | bool:
        if self.throttled:
            return False
        e_chat = (await self.directory()).get(chat_id)
        if forced:
            self.invalidate_chat(chat_id)
        if await self.get_chat(chat_id) is None:
            return False

        if e_chat and e_chat.pinned_message_id and not forced:
            return True

        text = await self.text(chat_id)
        if self.throttled:
            return False
        if e_chat and e_chat.pinned_message_id:
            with suppress(TelegramBadRequest):
                await self.bot.delete_message(chat_id, e_chat.pinned_message_id)

        pin_msg = await self.bot.send_message(chat_id, text)
        await pin_msg.pin(disable_notification=True)
        await self.db.patch_directory(chat_id, DirectoryPatch(pinned_message_id=pin_msg.message_id))
        self.invalidate_directory()
        return pin_msg

    async def update_pins(self, *, forced: bool = False) -> None:
        if self.throttled or (not forced and self._pins_lock.locked()):
            return
        async with self._pins_lock:
            if self.throttled or (not forced and monotonic() < self._pins_until):
                return
            if forced:
                self._chats.clear()
                self._uneditable_pins.clear()
                self._chat_epoch += 1
            epoch = self._chat_epoch
            with self.telemetry.operation(Boundary.PROVIDER, "ecosystem.refresh", provider=Provider.TELEGRAM) as observation:
                await self._update_pins()
                if self.throttled:
                    observation.set_outcome(Outcome.UNAVAILABLE)
                elif epoch == self._chat_epoch:
                    self._pins_until = monotonic() + 600

    async def _update_pins(self) -> None:
        await self.update_ic_members()
        if self.throttled:
            return

        e_chats = await self.directory(refresh=True)

        async def update_pin(ec: DirectoryRecord) -> None:
            if (
                not ec.pinned_message_id
                or self._uneditable_pins.get(ec.chat_id) == ec.pinned_message_id
                or await self.get_chat(ec.chat_id) is None
            ):
                return
            text = await self.text(ec.chat_id)
            epoch = self._chat_epoch
            try:
                await self.request(lambda: self.bot.edit_message_text(text, chat_id=ec.chat_id, message_id=ec.pinned_message_id))
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                if _chat_unavailable(error):
                    if epoch == self._chat_epoch:
                        self._chats[ec.chat_id] = None
                    return
                if isinstance(error, TelegramBadRequest):
                    reason = telegram_error_reason(error)
                    if reason == "message_not_found":
                        await self.db.clear_directory_pin(ec.chat_id, ec.pinned_message_id)
                        self.invalidate_directory()
                        return
                    if reason == "message_not_editable":
                        if epoch == self._chat_epoch:
                            self._uneditable_pins[ec.chat_id] = ec.pinned_message_id
                        return
                    if reason == "message_not_modified":
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
            epoch = self._chat_epoch
            try:
                members = await self.request(lambda: self.bot.get_chat_member_count(chat.id))
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                if not _chat_unavailable(error):
                    raise
                if epoch == self._chat_epoch:
                    self._chats[ec.chat_id] = None
                return None
            if members is not None and members != ec.members:
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
        async with self._chat_lock:
            if chat_id in self._chats:
                return self._chats[chat_id]
            epoch = self._chat_epoch
            try:
                chat = await self.request(lambda: self.bot.get_chat(chat_id))
                if chat is None:
                    return None
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                if not _chat_unavailable(error):
                    raise
                chat = None
            # Access can return later; never remove directory data for a failed lookup.
            if epoch == self._chat_epoch:
                self._chats[chat_id] = chat
            return chat

    async def chat_link(self, chat: Chat) -> str:
        if chat.username or chat.type == ChatType.PRIVATE:
            return await chat_link(chat)
        full = await self.get_chat(chat.id)
        url = await chat_url(full, force_link=True) if full is not None else None
        return hlink(chat.full_name, url) if url else chat.full_name

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

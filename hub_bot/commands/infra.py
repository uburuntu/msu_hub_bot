from contextlib import suppress
from typing import Any, Protocol, cast

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner, InlineQuery, InlineQueryResultArticle, InputTextMessageContent, Message
from aiogram.utils.markdown import hbold

from common.db.edb import EdgeDB
from common.tg.runtime import gather_complete
from common.tg.utils import chat_link, sender_mention
from common.tg.wrapper import BotWrapper
from hub_bot.db import EcosystemChat
from hub_bot.events import EcosystemManager


class _DirectoryMutations(Protocol):
    async def insert(self, **values: Any) -> object: ...
    async def delete(self, pk: int) -> object: ...


async def process_create_infra_chat(message: Message, bot: BotWrapper, db: EdgeDB, events_chat_id: int) -> Message | bool | None:
    if message.chat.type == ChatType.PRIVATE or await EcosystemChat.query(db).exist(message.chat.id):
        return True
    admins = await bot.get_chat_administrators(message.chat.id)
    me = next((admin for admin in admins if admin.user.id == bot.id), None)
    if not isinstance(me, ChatMemberAdministrator):
        return None
    if me.can_delete_messages:
        await message.delete()
    if not me.can_promote_members:
        return None
    await cast(_DirectoryMutations, EcosystemChat.query(db)).insert(
        chat_id=message.chat.id,
        name=message.chat.full_name,
        section=EcosystemManager.ChatGroup.other.name,
        members=await bot.get_chat_member_count(message.chat.id),
    )
    event = f"❇️ {sender_mention(message)} добавил в экосистему новый чат: {await chat_link(message.chat, True)}."
    return await bot.send_message(events_chat_id, event) if events_chat_id else None


async def process_delete_infra_chat(message: Message, bot: BotWrapper, db: EdgeDB, events_chat_id: int) -> Message | bool | None:
    if message.chat.type == ChatType.PRIVATE or not await EcosystemChat.query(db).exist(message.chat.id):
        return True
    admins = await bot.get_chat_administrators(message.chat.id)
    me = next((admin for admin in admins if admin.user.id == bot.id), None)
    if isinstance(me, ChatMemberAdministrator) and me.can_delete_messages:
        await message.delete()
    await cast(_DirectoryMutations, EcosystemChat.query(db)).delete(pk=message.chat.id)
    event = f"❎️ {sender_mention(message)} удалил чат из экосистемы: {await chat_link(message.chat, True)}."
    return await bot.send_message(events_chat_id, event) if events_chat_id else None


async def process_update_pins(_message: Message, em: EcosystemManager) -> None:
    await em.update_pins()


async def process_pin(message: Message, bot: BotWrapper, em: EcosystemManager) -> Message | bool | None:
    admins = await bot.get_chat_administrators(message.chat.id)
    me = next((admin for admin in admins if admin.user.id == bot.id), None)
    if not isinstance(me, ChatMemberAdministrator):
        return None
    if me.can_delete_messages:
        await message.delete()
    if me.can_pin_messages:
        return await em.pin(message.chat.id, forced=True)
    return None


async def process_pin_all(_message: Message, db: EdgeDB, em: EcosystemManager) -> list[Message | bool]:
    chats = await EcosystemChat.query(db).get_all_cached()
    return await gather_complete(*(em.pin(chat.chat_id) for chat in chats.values()))


async def process_links(message: Message, em: EcosystemManager) -> Message:
    return await message.answer(await em.text())


async def process_status(message: Message, bot: BotWrapper, db: EdgeDB) -> Message:
    chats = await EcosystemChat.query(db).get_all_cached()
    completed: list[tuple[int, str, str]] = []

    async def check(chat_id: int) -> None:
        try:
            member = await bot.get_chat_member(chat_id, bot.id)
            if isinstance(member, ChatMemberOwner):
                result = (chat_id, "✅", "✅")
            elif isinstance(member, ChatMemberAdministrator):
                result = (chat_id, "✅", "✅" if member.can_promote_members else "❌")
            else:
                result = (chat_id, "❌", "❌")
        except TelegramBadRequest:
            result = (chat_id, "💔", "💔")
        completed.append(result)

    await gather_complete(*(check(chat_id) for chat_id in chats))
    text = hbold("Status") + "\n\n"
    for chat_id, first, second in completed:
        text += f"— {chats[chat_id].name}: {first} {second}\n"
    return await message.reply(text)


async def process_inline(inline_query: InlineQuery, em: EcosystemManager) -> bool:
    text = await em.text()
    content = InputTextMessageContent(message_text=text)
    item = InlineQueryResultArticle(id="0", title="Все чаты МГУ", input_message_content=content)
    with suppress(TelegramBadRequest):
        return await inline_query.answer(results=[item], is_personal=False, cache_time=10 * 60)
    return True

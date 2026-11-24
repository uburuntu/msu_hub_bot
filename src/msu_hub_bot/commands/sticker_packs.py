"""/packs: list a chat's or user's sticker packs and let their editors rename them or set covers."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    Chat,
    InputRichBlockButtons,
    InputRichBlockFooter,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockUnion,
    InputRichMessage,
    Message,
    ReplyParameters,
    RichMessageButton,
    RichTextUrl,
    StickerSet,
)
from pydantic import BaseModel

from msu_hub_bot.commands.sticker import Stickers, sticker_set_name_template, sticker_set_name_template_a
from msu_hub_bot.commands.sticker_emoji import preview_source
from msu_hub_bot.commands.sticker_input import custom_emoji_source
from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.media.sticker_media import MAX_INPUT_BYTES, StickerMediaError, prepare_cover
from msu_hub_bot.telegram.files import DownloadTooLarge, download_by_file_id, input_file
from msu_hub_bot.telegram.sticker_sets import MAX_PACK_STICKERS, MAX_SERIES_PACKS, series_pack_name, sticker_error

logger = logging.getLogger(__name__)
MAX_TITLE_LENGTH = 64
COVER_TIMEOUT = 30
# Index 0 is the legacy animated pack; series packs count from 1.
LEGACY_INDEX = 0


class PackCallback(CallbackData, prefix="pk"):
    action: str
    owner: int
    index: int


class PackStates(StatesGroup):
    title = State()
    cover = State()


class PackEdit(BaseModel):
    """The pack being edited and the menu to update afterwards."""

    owner: int
    index: int
    chat_id: int
    menu_message_id: int | None = None


@dataclass(frozen=True)
class Pack:
    index: int
    name: str
    title: str
    count: int
    still: int = 0
    animated: int = 0
    video: int = 0
    size: int = 0

    @classmethod
    def read(cls, index: int, name: str, found: StickerSet) -> "Pack":
        stickers = found.stickers
        animated = sum(1 for sticker in stickers if sticker.is_animated)
        video = sum(1 for sticker in stickers if sticker.is_video)
        size = sum(sticker.file_size or 0 for sticker in stickers)
        return cls(index, name, found.title, len(stickers), len(stickers) - animated - video, animated, video, size)

    def details(self) -> str:
        """«87 из 120 · 🖼 52 · ✨ 30 · 🎞 5 · 9,4 МБ», skipping empty formats."""
        parts = [f"{self.count} из {MAX_PACK_STICKERS}"]
        parts += [f"{mark} {amount}" for mark, amount in (("🖼", self.still), ("✨", self.animated), ("🎞", self.video)) if amount]
        if self.size:
            parts.append(file_size(self.size))
        return " · ".join(parts)


def plural(amount: int, one: str, few: str, many: str) -> str:
    tens, units = amount % 100, amount % 10
    if units == 1 and tens != 11:
        return one
    if 2 <= units <= 4 and not 12 <= tens <= 14:
        return few
    return many


def file_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} КБ"
    return f"{size / 1024 / 1024:.1f} МБ".replace(".", ",")


def totals(packs: Sequence[Pack]) -> str:
    """«3 пака · 254 стикера · места ещё на 966» across the whole series."""
    stickers = sum(pack.count for pack in packs)
    series = sum(pack.count for pack in packs if pack.index != LEGACY_INDEX)
    room = MAX_SERIES_PACKS * MAX_PACK_STICKERS - series
    parts = [
        f"{len(packs)} {plural(len(packs), 'пак', 'пака', 'паков')}",
        f"{stickers} {plural(stickers, 'стикер', 'стикера', 'стикеров')}",
        f"места ещё на {room}" if room > 0 else "все места заняты",
    ]
    return " · ".join(parts)


def pack_name(owner: int, index: int) -> str:
    if index == LEGACY_INDEX:
        return sticker_set_name_template_a.format(id=owner)
    return series_pack_name(sticker_set_name_template.format(id=owner), index)


def pack_link(name: str) -> str:
    return f"https://t.me/addstickers/{name}"


def owner_for(chat: Chat, user_id: int) -> int:
    return user_id if chat.type == ChatType.PRIVATE else abs(chat.id)


async def may_edit(bot: Bot, chat: Chat, user_id: int, owner: int) -> bool:
    """Owners edit personal packs in DM; any administrator edits the chat's packs there."""
    if chat.type == ChatType.PRIVATE:
        return owner == user_id
    if owner != abs(chat.id):
        return False
    admins = await bot.get_chat_administrators(chat_id=chat.id)
    return user_id in (admin.user.id for admin in admins)


async def _lookup(bot: Bot, name: str) -> StickerSet | None:
    try:
        return await bot.get_sticker_set(name)
    except TelegramBadRequest as error:
        if sticker_error(error, "STICKERSET_INVALID"):
            return None
        raise


async def read_packs(bot: Bot, owner: int) -> list[Pack]:
    packs = []
    for index in range(1, MAX_SERIES_PACKS + 1):
        name = pack_name(owner, index)
        if (found := await _lookup(bot, name)) is None:
            break
        packs.append(Pack.read(index, name, found))
    legacy = pack_name(owner, LEGACY_INDEX)
    if (found := await _lookup(bot, legacy)) is not None:
        packs.append(Pack.read(LEGACY_INDEX, legacy, found))
    return packs


def _button(text: str, action: str, owner: int, index: int) -> RichMessageButton:
    return RichMessageButton(text=text, callback_data=PackCallback(action=action, owner=owner, index=index).pack())


def render_list(packs: Sequence[Pack], owner: int, *, personal: bool, editable: bool, notice: str = "") -> InputRichMessage:
    blocks: list[InputRichBlockUnion] = [
        InputRichBlockSectionHeading(text="Ваши стикерпаки" if personal else "Стикерпаки чата", size=2),
        InputRichBlockParagraph(text=totals(packs)),
    ]
    if notice:
        blocks.append(InputRichBlockParagraph(text=notice))
    for pack in packs:
        count = pack.details()
        if pack.index == LEGACY_INDEX:
            count = f"Старый анимированный пак · {count}"
        buttons = [RichMessageButton(text="Открыть", url=pack_link(pack.name))]
        if editable:
            buttons += [_button("✏️ Название", "rename", owner, pack.index), _button("🖼 Обложка", "cover", owner, pack.index)]
        blocks += [
            InputRichBlockSectionHeading(text=RichTextUrl(text=pack.title, url=pack_link(pack.name)), size=4),
            InputRichBlockParagraph(text=count),
            InputRichBlockButtons(buttons=buttons),
        ]
    if any(pack.animated or pack.video for pack in packs):
        blocks.append(InputRichBlockFooter(text="🖼 картинки · ✨ анимация · 🎞 видео"))
    return InputRichMessage(blocks=blocks, skip_entity_detection=True)


def render_rename(title: str, owner: int, index: int) -> InputRichMessage:
    return InputRichMessage(
        blocks=[
            InputRichBlockSectionHeading(text=f"✏️ Новое название для «{title}»", size=3),
            InputRichBlockParagraph(text=f"Пришлите его сообщением, до {MAX_TITLE_LENGTH} символов. Его увидят все, кто добавил пак."),
            InputRichBlockButtons(buttons=[_button("Отмена", "back", owner, index)]),
        ],
        skip_entity_detection=True,
    )


def render_cover(title: str, owner: int, index: int, *, personal: bool) -> InputRichMessage:
    return InputRichMessage(
        blocks=[
            InputRichBlockSectionHeading(text=f"🖼 Обложка для «{title}»", size=3),
            InputRichBlockParagraph(
                text="Пришлите картинку, стикер или кастомный эмодзи — сделаю из них обложку. От анимации останется первый кадр."
            ),
            InputRichBlockButtons(
                buttons=[
                    _button("Моё фото" if personal else "Фото чата", "photo", owner, index),
                    _button("Сбросить", "reset", owner, index),
                    _button("Отмена", "back", owner, index),
                ]
            ),
        ],
        skip_entity_detection=True,
    )


def empty_text(personal: bool) -> str:
    if personal:
        return "У вас пока нет стикерпака. Ответьте /s на картинку, GIF, видео или стикер — и он появится."
    return "У чата пока нет стикерпака. Админы могут начать его, ответив /sc на картинку, GIF, видео или стикер."


async def process_packs(message: Message, bot: Bot) -> Message | bool:
    user = message.from_user
    if user is None or message.sender_chat is not None:
        return True
    personal = message.chat.type == ChatType.PRIVATE
    owner = owner_for(message.chat, user.id)
    packs = await read_packs(bot, owner)
    if not packs:
        return await message.reply(empty_text(personal))
    editable = await may_edit(bot, message.chat, user.id, owner)
    rich = render_list(packs, owner, personal=personal, editable=editable)
    thread = message.message_thread_id if message.is_topic_message else None
    return await bot.send_rich_message(
        chat_id=message.chat.id,
        rich_message=rich,
        message_thread_id=thread,
        reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
    )


async def show(bot: Bot, edit: PackEdit, user_id: int, rich: InputRichMessage) -> bool:
    """Update the menu in place; False if it is gone, e.g. deleted."""
    try:
        if edit.menu_message_id is not None:
            await bot.edit_message_text(chat_id=edit.chat_id, message_id=edit.menu_message_id, rich_message=rich)
        else:
            return False
    except TelegramBadRequest as error:
        if sticker_error(error, "message is not modified"):
            return True
        logger.warning("Sticker pack menu could not be updated")
        return False
    return True


async def show_list(bot: Bot, chat: Chat, edit: PackEdit, user_id: int, notice: str = "") -> bool:
    packs = await read_packs(bot, edit.owner)
    personal = chat.type == ChatType.PRIVATE
    return await show(bot, edit, user_id, render_list(packs, edit.owner, personal=personal, editable=True, notice=notice))


async def current_edit(state: FSMContext) -> PackEdit | None:
    data = await state.get_data()
    try:
        return PackEdit.model_validate(data.get("pack_edit") or {})
    except ValueError:
        return None


def cover_rejection(index: int) -> str:
    if index == LEGACY_INDEX:
        return "Telegram не принял обложку: у старого анимированного пака она, похоже, должна быть анимацией."
    return "Telegram не принял обложку. Попробуйте другую картинку."


async def set_cover(bot: Bot, edit: PackEdit, user_id: int, payload: bytes | None) -> str:
    """Return the notice for the menu."""
    name = pack_name(edit.owner, edit.index)
    try:
        if payload is None:
            await bot.set_sticker_set_thumbnail(name=name, user_id=user_id, format="static")
            return "✅ Обложка сброшена: теперь это первый стикер пака."
        await bot.set_sticker_set_thumbnail(name=name, user_id=user_id, format="static", thumbnail=input_file(payload, "cover.webp"))
    except TelegramBadRequest:
        return cover_rejection(edit.index)
    return "✅ Обложка обновлена. У других она появится в течение часа."


async def convert_cover(file_id: str, bot: Bot, cpu_executor: TPExecutor) -> bytes:
    try:
        file = await download_by_file_id(file_id, bot, max_bytes=MAX_INPUT_BYTES)
    except DownloadTooLarge:
        raise StickerMediaError("Файл больше 20 МБ. Пришлите картинку поменьше.") from None
    if file is None:
        raise StickerMediaError("Не удалось скачать картинку. Попробуйте ещё раз.")
    with file:
        data = file.getvalue()
    payload, timeouted = await cpu_executor.run(prepare_cover, data, timeout=COVER_TIMEOUT)
    if timeouted:
        raise StickerMediaError("Обработка заняла слишком много времени. Пришлите картинку попроще.")
    return bytes(payload)


async def profile_photo_id(bot: Bot, chat: Chat, user_id: int) -> str | None:
    if chat.type == ChatType.PRIVATE:
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
        return photos.photos[0][-1].file_id if photos.photos else None
    photo = (await bot.get_chat(chat_id=chat.id)).photo
    return photo.big_file_id if photo else None


async def process_packs_cb(
    query: CallbackQuery, callback_data: PackCallback, state: FSMContext, bot: Bot, cpu_executor: TPExecutor
) -> None:
    message = query.message
    if not isinstance(message, Message):
        await query.answer("Это меню устарело. Вызовите /packs ещё раз.", show_alert=True)
        return
    owner, index = callback_data.owner, callback_data.index
    if not LEGACY_INDEX <= index <= MAX_SERIES_PACKS or not await may_edit(bot, message.chat, query.from_user.id, owner):
        await query.answer("Менять стикерпаки чата могут только его админы.", show_alert=True)
        return
    personal = message.chat.type == ChatType.PRIVATE
    edit = PackEdit(
        owner=owner,
        index=index,
        chat_id=message.chat.id,
        menu_message_id=message.message_id,
    )
    user_id = query.from_user.id
    action = callback_data.action
    if action == "back":
        await state.clear()
        await query.answer()
        await show_list(bot, message.chat, edit, user_id)
        return
    pack = await _lookup(bot, pack_name(owner, index))
    if pack is None:
        await state.clear()
        await query.answer("Этого стикерпака уже нет.", show_alert=True)
        await show_list(bot, message.chat, edit, user_id)
        return
    if action in ("rename", "cover"):
        await state.set_state(PackStates.title if action == "rename" else PackStates.cover)
        await state.set_data({"pack_edit": edit.model_dump(mode="json")})
        await query.answer()
        rich = render_rename(pack.title, owner, index) if action == "rename" else render_cover(pack.title, owner, index, personal=personal)
        await show(bot, edit, user_id, rich)
        return
    if action == "reset":
        await query.answer()
        notice = await set_cover(bot, edit, user_id, None)
    elif action == "photo":
        file_id = await profile_photo_id(bot, message.chat, user_id)
        if file_id is None:
            await query.answer("У вас нет фото профиля или оно скрыто." if personal else "У чата нет фото.", show_alert=True)
            return
        await query.answer("Делаю обложку…")
        try:
            notice = await set_cover(bot, edit, user_id, await convert_cover(file_id, bot, cpu_executor))
        except StickerMediaError as error:
            notice = str(error)
    else:
        await query.answer()
        return
    await state.clear()
    await show_list(bot, message.chat, edit, user_id, notice)


async def editing(message: Message, state: FSMContext, bot: Bot) -> PackEdit | None:
    """The stored edit, if the sender may still change that pack in this chat."""
    edit = await current_edit(state)
    if edit is None or message.from_user is None or edit.chat_id != message.chat.id:
        await state.clear()
        return None
    if not await may_edit(bot, message.chat, message.from_user.id, edit.owner):
        await state.clear()
        await message.reply("Менять стикерпаки чата могут только его админы.")
        return None
    return edit


async def process_pack_title(message: Message, state: FSMContext, bot: Bot) -> Message | bool:
    if (edit := await editing(message, state, bot)) is None or message.from_user is None:
        return True
    title = (message.text or "").strip()
    if not 1 <= len(title) <= MAX_TITLE_LENGTH:
        return await message.reply(f"Название должно быть от 1 до {MAX_TITLE_LENGTH} символов. Или /cancel.")
    try:
        await bot.set_sticker_set_title(name=pack_name(edit.owner, edit.index), title=title)
    except TelegramBadRequest:
        return await message.reply("Telegram не принял это название. Попробуйте другое или /cancel.")
    await state.clear()
    notice = "✅ Название обновлено."
    if not await show_list(bot, message.chat, edit, message.from_user.id, notice):
        return await message.answer(notice)
    return True


async def process_pack_cover(message: Message, state: FSMContext, bot: Bot, cpu_executor: TPExecutor) -> Message | bool:
    if (edit := await editing(message, state, bot)) is None or message.from_user is None:
        return True
    try:
        source, kind = Stickers.source_media(message)
        if source is None:
            source = await custom_emoji_source(message, bot)
            kind = "static" if source is not None and not (source.is_animated or source.is_video) else "video"
        if source is None:
            return await message.reply("Пришлите картинку, стикер или кастомный эмодзи. Или /cancel.")
        # Animations become their first frame: Telegram's own still preview.
        still = source if kind == "static" else preview_source(source)
        if still is None:
            return await message.reply("Не нашёл кадр для обложки. Пришлите картинку.")
        if (still.file_size or 0) > MAX_INPUT_BYTES:
            raise StickerMediaError("Файл больше 20 МБ. Пришлите картинку поменьше.")
        payload = await convert_cover(still.file_id, bot, cpu_executor)
    except StickerMediaError as error:
        return await message.reply(f"{error} Или /cancel.")
    notice = await set_cover(bot, edit, message.from_user.id, payload)
    await state.clear()
    if not await show_list(bot, message.chat, edit, message.from_user.id, notice):
        return await message.answer(notice)
    return True

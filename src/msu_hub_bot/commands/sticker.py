import io
from typing import Any

import emoji
from PIL import Image, ImageOps
from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message, InputSticker
from aiogram.utils.markdown import hlink
from pydantic import BaseModel

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.telegram.extraction import Extractor
from msu_hub_bot.telegram.files import DownloadableMedia, download, download_by_file_id, input_file
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.state import ReleasableEventIsolation, UpdateStateContext, release_state_isolation
from msu_hub_bot.utils import image_bytes_io
from msu_hub_bot.media.sticker_media import MAX_INPUT_BYTES, StickerMediaError, prepare_media
from msu_hub_bot.telegram.sticker_sets import StickerSetClient, UploadedSticker, UploadMetadata, sticker_error

sticker_set_name_template = "with_love_for_{id}_by_msu_hub_bot"
sticker_set_name_template_a = "with_love_for_{id}a_by_msu_hub_bot"
trimmed_sticker_notice = "✂️ Для стикера использованы только первые 7 секунд."


class StickerStates(StatesGroup):
    sticker_set_name = State()


class PersonalStickerDraft(BaseModel):
    sticker_set_name: str
    emojis: str
    png: str = ""
    tgs: str = ""


class ChatStickerDraft(BaseModel):
    mixed_sticker: InputSticker
    sticker_upload: UploadMetadata
    sticker_set_name: str
    sticker_chat_id: int
    sticker_user_id: int
    sticker_origin_message_id: int
    sticker_trimmed: bool = False


# A title retry may overlap /cancel or a different conversation in the same topic.
_pending_saves: set[tuple[StorageKey, int]] = set()


def only_emojis(text: str) -> str:
    d = {e["emoji"]: True for e in emoji.emoji_list(text)}
    return "".join(list(d)[:5])


def personal_input(png: io.BytesIO | None, tgs: io.BytesIO | None, emojis: str) -> InputSticker:
    source = tgs if tgs is not None else png
    if source is None:
        raise StickerMediaError("Не удалось скачать файл. Стикер не добавлен.")
    return InputSticker(
        sticker=input_file(source, "sticker.tgs" if tgs is not None else "sticker.png"),
        format="animated" if tgs is not None else "static",
        emoji_list=[item["emoji"] for item in emoji.emoji_list(emojis)],
    )


class Stickers:
    @classmethod
    async def tgs(cls, message: Message, bot: Bot) -> tuple[str, io.BytesIO] | None:
        target = message.reply_to_message
        if target and target.sticker and target.sticker.is_animated:
            file = await download(target.sticker, bot)
            if file is not None:
                return target.sticker.file_id, file
        return None

    @staticmethod
    def png_cut(f: io.BytesIO, squared: bool = False) -> io.BytesIO:
        image = Image.open(f)
        if squared:
            box = (512, 512)
        else:
            box = (512, image.height * 512 // image.width)
            if image.width < image.height:
                box = (image.width * 512 // image.height, 512)
        image = ImageOps.fit(image, box, Image.Resampling.LANCZOS)
        return image_bytes_io(image, "sticker", "png")

    @classmethod
    async def png(cls, message: Message, bot: Bot) -> tuple[str, io.BytesIO] | None:
        _, dest = await Extractor.image(message, with_profile_photo=True)
        file = await download(dest, bot)
        if dest is not None and file is not None:
            return dest.file_id, cls.png_cut(file)
        return None

    @classmethod
    async def sticker_set_name(
        cls,
        message: Message,
        state: FSMContext,
        bot: Bot,
        state_context: UpdateStateContext,
        events_isolation: ReleasableEventIsolation,
    ) -> Message | bool:
        data = await state.get_data()
        if data.get("mixed_sticker"):
            return await cls.finish_chat_set(message, state, data, bot, state_context, events_isolation)
        if message.from_user is None:
            return True
        title = message.text or message.caption or ""
        if not title:
            return await message.reply("🎈 Выберите подходящее название для стикерпака или тыкните /cancel")
        draft = PersonalStickerDraft.model_validate(data)
        await state.clear()
        release_state_isolation(state_context)
        png_source = await download_by_file_id(draft.png, bot) if draft.png else None
        png = cls.png_cut(png_source) if png_source is not None else None
        tgs = await download_by_file_id(draft.tgs, bot) if draft.tgs else None
        try:
            sticker = personal_input(png, tgs, draft.emojis)
            await bot.create_new_sticker_set(
                user_id=message.from_user.id,
                name=draft.sticker_set_name,
                title=title,
                stickers=[sticker],
            )
            link = hlink("стикерпак", f"https://t.me/addstickers/{draft.sticker_set_name}")
            await message.reply(
                f"✨ Ура, для вас был создан {link}. Управлять им можно через @Stickers.\n\n"
                "Имейте в виду, что на телефонах новые стикеры появляются с задержкой."
            )
            pack = await bot.get_sticker_set(draft.sticker_set_name)
            return await message.reply_sticker(pack.stickers[-1].file_id)
        except StickerMediaError as exc:
            return await message.reply(str(exc))
        except TelegramBadRequest as exc:
            if sticker_error(exc, "PEER_ID_INVALID"):
                return await message.reply("🤷🏻‍♂️ Чтоб я смог создать стикерпак для вас, вам нужно начать личный чат со мной")
            await message.reply("🤷🏻‍♂️ Произошла какая-то ошибка, подробнее в /error_stickers")
            raise

    @classmethod
    async def make_sticker(
        cls,
        message: Message,
        meta: MetaInfo,
        state: FSMContext,
        sticker_set_name: str,
        bot: Bot,
        png: tuple[str, io.BytesIO] | None = None,
        tgs: tuple[str, io.BytesIO] | None = None,
    ) -> Message | bool:
        if message.from_user is None:
            return True
        emojis = only_emojis(meta.extract_text()[1]) or "✨"
        try:
            try:
                await bot.get_sticker_set(sticker_set_name)
            except TelegramBadRequest as exc:
                if not sticker_error(exc, "STICKERSET_INVALID"):
                    raise
                draft = PersonalStickerDraft(
                    sticker_set_name=sticker_set_name, emojis=emojis, png=png[0] if png else "", tgs=tgs[0] if tgs else ""
                )
                await state.set_data(draft.model_dump(mode="json"))
                await state.set_state(StickerStates.sticker_set_name)
                return await message.reply(
                    "🎈 Для вас еще не создан стикерпак. Придумайте ему название в следующем сообщении ⬇️, или тыкните /cancel. "
                    "Учтите, что название стикерпака видят все и изменить его нельзя."
                )
            await bot.add_sticker_to_set(
                user_id=message.from_user.id,
                name=sticker_set_name,
                sticker=personal_input(png[1] if png else None, tgs[1] if tgs else None, emojis),
            )
        except TelegramBadRequest as exc:
            if sticker_error(exc, "STICKERS_TOO_MUCH"):
                return await message.reply("🤷🏻‍♂️ Стикерпак заполнен. Удалить ненужный стикер можно командой /sd ответом на него.")
            await message.reply("🤷🏻‍♂️ Произошла какая-то ошибка, подробнее в /error_stickers")
            raise
        pack = await bot.get_sticker_set(sticker_set_name)
        return await message.reply_sticker(pack.stickers[-1].file_id)

    @staticmethod
    def source_media(message: Message) -> tuple[DownloadableMedia | None, str | None]:
        for target in (message, message.reply_to_message):
            if target is None:
                continue
            if target.sticker:
                sticker = target.sticker
                if sticker.type != "regular":
                    raise StickerMediaError("Пришлите обычный стикер, а не маску или custom emoji.")
                kind = "animated" if sticker.is_animated else "video" if sticker.is_video else "static"
                return sticker, kind
            if target.animation or target.video or target.video_note:
                return target.animation or target.video or target.video_note, "video"
            if target.photo:
                return target.photo[-1], "static"
            if target.document:
                doc = target.document
                mime = (doc.mime_type or "").lower()
                if mime.startswith("video/") or mime == "image/gif":
                    return doc, "video"
                if mime.startswith("image/"):
                    return doc, "static"
                raise StickerMediaError("Формат файла не поддерживается. Пришлите картинку, GIF, видео или готовый Telegram-стикер.")
        return None, None

    @classmethod
    async def make_chat_sticker(
        cls,
        message: Message,
        meta: MetaInfo,
        state: FSMContext,
        name: str,
        bot: Bot,
        cpu_executor: TPExecutor,
    ) -> Message | bool:
        if message.from_user is None:
            return True
        try:
            source, kind = cls.source_media(message)
            if source is None:
                _, source = await Extractor.image(message, with_profile_photo=True)
                kind = "static"
            if source is None:
                return await message.reply("Ответьте /sc на картинку, GIF, видео или стикер.")
            if (source.file_size or 0) > MAX_INPUT_BYTES:
                raise StickerMediaError("Файл больше 20 МБ. Стикер не добавлен.")
            file = await download(source, bot)
            if file is None:
                raise StickerMediaError("Не удалось скачать файл. Стикер не добавлен.")
            prepared, timeouted = await cpu_executor.run(prepare_media, file.getvalue(), kind or "static")
            if timeouted:
                raise StickerMediaError("Обработка заняла слишком много времени. Стикер не добавлен.")
            emojis = list(dict.fromkeys(e["emoji"] for e in emoji.emoji_list(meta.extract_text()[1])))[:5] or ["✨"]
            client = StickerSetClient(bot)
            uploaded = await client.upload(message.from_user.id, prepared.payload, prepared.kind, emojis)
            if not await client.save(name, message.from_user.id, uploaded):
                draft = ChatStickerDraft(
                    mixed_sticker=uploaded.input_sticker(),
                    sticker_upload=UploadMetadata.model_validate(uploaded.metadata()),
                    sticker_set_name=name,
                    sticker_chat_id=message.chat.id,
                    sticker_user_id=message.from_user.id,
                    sticker_origin_message_id=message.message_id,
                    sticker_trimmed=prepared.trimmed,
                )
                await state.set_data(draft.model_dump(mode="json"))
                await state.set_state(StickerStates.sticker_set_name)
                return await message.reply("🎈 Пришлите название стикерпака (1–64 символа) или /cancel.")
            return await cls.reply_saved_sticker(message, client, name, uploaded, trimmed=prepared.trimmed)
        except StickerMediaError as exc:
            return await message.reply(str(exc))
        except TelegramBadRequest as exc:
            if sticker_error(exc, "PEER_ID_INVALID"):
                return await message.reply("Сначала начните личный чат со мной, затем повторите /sc.")
            await message.reply("Не удалось добавить стикер. Подробнее в /error_stickers.")
            raise

    @classmethod
    async def reply_saved_sticker(
        cls,
        message: Message,
        client: StickerSetClient,
        name: str,
        uploaded: UploadedSticker,
        show_link: bool = False,
        trimmed: bool = False,
    ) -> Message:
        # A failed preview must never repeat the save.
        file_id = await client.resolve(name, uploaded)
        link = hlink("стикерпак", f"https://t.me/addstickers/{name}")
        if file_id is not None:
            try:
                reply = await message.reply_sticker(file_id)
            except TelegramBadRequest:
                pass
            else:
                notices = []
                if show_link:
                    notices.append(f"✨ Стикерпак чата: {link}")
                if trimmed:
                    notices.append(trimmed_sticker_notice)
                if notices:
                    await message.reply("\n".join(notices))
                return reply
        text = f"✨ Стикер добавлен в {link}. Откройте его в паке."
        if trimmed:
            text += "\n" + trimmed_sticker_notice
        return await message.reply(text)

    @classmethod
    async def finish_chat_set(
        cls,
        message: Message,
        state: FSMContext,
        data: dict[str, Any],
        bot: Bot,
        state_context: UpdateStateContext,
        events_isolation: ReleasableEventIsolation,
    ) -> Message:
        draft = ChatStickerDraft.model_validate(data)
        if message.from_user is None or message.chat.id != draft.sticker_chat_id or message.from_user.id != draft.sticker_user_id:
            return await message.reply("Название должен прислать автор команды в том же чате.")
        if not await can_edit_chat_stickers(message, bot):
            return await message.reply("Стикерпак чата могут редактировать только его админы.")
        title = (message.text or message.caption or "").strip()
        if not 1 <= len(title) <= 64:
            return await message.reply("Название должно содержать от 1 до 64 символов. Или /cancel.")
        guard = state.key, draft.sticker_origin_message_id
        if guard in _pending_saves:
            return await message.reply("Стикер уже сохраняется. Немного подождите.")
        client = StickerSetClient(bot)
        uploaded = UploadedSticker.from_pending(data)
        _pending_saves.add(guard)
        release_state_isolation(state_context)
        try:
            try:
                await client.save(draft.sticker_set_name, message.from_user.id, uploaded, title=title)
            except TelegramBadRequest as exc:
                if sticker_error(exc, "PEER_ID_INVALID"):
                    return await message.reply("Начните личный чат со мной и снова пришлите название.")
                await message.reply("Не удалось сохранить стикер. Попробуйте ещё раз или /cancel. Подробнее в /error_stickers.")
                raise
            async with events_isolation.lock(state.key):
                if await state.get_state() == StickerStates.sticker_set_name.state and await state.get_data() == data:
                    await state.clear()
            return await cls.reply_saved_sticker(
                message, client, draft.sticker_set_name, uploaded, show_link=True, trimmed=draft.sticker_trimmed
            )
        finally:
            _pending_saves.discard(guard)

    @classmethod
    async def make_sticker_png(
        cls,
        message: Message,
        meta: MetaInfo,
        state: FSMContext,
        sticker_set_name: str,
        bot: Bot,
    ) -> Message | bool | None:
        sticker = await cls.png(message, bot)
        if sticker:
            return await cls.make_sticker(message, meta, state, sticker_set_name, bot, png=sticker)
        return None

    @classmethod
    async def make_sticker_tgs(
        cls,
        message: Message,
        meta: MetaInfo,
        state: FSMContext,
        sticker_set_name: str,
        bot: Bot,
    ) -> Message | bool | None:
        sticker = await cls.tgs(message, bot)
        if sticker:
            return await cls.make_sticker(message, meta, state, sticker_set_name, bot, tgs=sticker)
        return None


async def process_sticker(message: Message, meta: MetaInfo, state: FSMContext, bot: Bot) -> Message | bool | None:
    if message.from_user is None:
        return True
    name = sticker_set_name_template.format(id=message.from_user.id)
    return await Stickers.make_sticker_png(message, meta, state, name, bot)


async def can_edit_chat_stickers(message: Message, bot: Bot) -> bool:
    if message.chat.type == ChatType.PRIVATE or not message.from_user or message.sender_chat:
        return False
    admins = await bot.get_chat_administrators(chat_id=message.chat.id)
    return message.from_user.id in (a.user.id for a in admins)


async def process_sticker_chat(
    message: Message,
    meta: MetaInfo,
    state: FSMContext,
    bot: Bot,
    cpu_executor: TPExecutor,
) -> Message | bool:
    if message.chat.type == ChatType.PRIVATE:
        return await message.reply("🤷🏻‍♂️ Эта команда только для чатов")
    if not await can_edit_chat_stickers(message, bot):
        return await message.reply("🤷🏻‍♂️ Стикерпак чата могут редактировать только его админы")
    name = sticker_set_name_template.format(id=abs(message.chat.id))
    return await Stickers.make_chat_sticker(message, meta, state, name, bot, cpu_executor)


async def process_animated_sticker(message: Message, meta: MetaInfo, state: FSMContext, bot: Bot) -> Message | bool | None:
    if message.from_user is None:
        return True
    name = sticker_set_name_template_a.format(id=message.from_user.id)
    return await Stickers.make_sticker_tgs(message, meta, state, name, bot)


async def process_animated_sticker_chat(message: Message, meta: MetaInfo, state: FSMContext, bot: Bot) -> Message | bool | None:
    if message.chat.type == ChatType.PRIVATE:
        return await message.reply("🤷🏻‍♂️ Эта команда только для чатов")
    if not await can_edit_chat_stickers(message, bot):
        return await message.reply("🤷🏻‍♂️ Стикерпак чата могут редактировать только его админы")
    name = sticker_set_name_template_a.format(id=abs(message.chat.id))
    return await Stickers.make_sticker_tgs(message, meta, state, name, bot)


async def process_sticker_delete(message: Message, bot: Bot) -> Message | bool:
    if message.from_user is None or not (target := message.reply_to_message) or not (sticker := target.sticker):
        return True
    name = sticker.set_name or ""
    link = hlink("пака", f"https://t.me/addstickers/{name}")
    if not name.endswith("_by_msu_hub_bot"):
        return await message.reply("🤷🏻‍♂️ Этот стикерпак создан не мной, попробуйте через @Stickers")
    if name in (sticker_set_name_template.format(id=message.from_user.id), sticker_set_name_template_a.format(id=message.from_user.id)):
        await bot.delete_sticker_from_set(sticker=sticker.file_id)
        return await message.reply(f"✅ Стикер удален из {link}, в течение часа он пропадет из набора у всех пользователей")
    if name in (sticker_set_name_template.format(id=abs(message.chat.id)), sticker_set_name_template_a.format(id=abs(message.chat.id))):
        if not await can_edit_chat_stickers(message, bot):
            return await message.reply("🤷🏻‍♂️ Стикерпак чата могут редактировать только его админы")
        await bot.delete_sticker_from_set(sticker=sticker.file_id)
        return await message.reply(f"✅ Стикер удален из {link}, в течение часа он пропадет из набора у всех пользователей")
    return await message.reply(
        "🤷🏻‍♂️ Судя по всему, стикерпак создан другим пользователем или в другом чате, где создавали — там и удаляйте"
    )

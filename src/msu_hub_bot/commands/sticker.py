from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message, InputSticker, Sticker
from aiogram.utils.markdown import hlink
from pydantic import BaseModel

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.commands.sticker_emoji import StickerEmojiPicker
from msu_hub_bot.commands.sticker_input import StickerMetadata, custom_emoji_source, parse_metadata, reusable_sticker
from msu_hub_bot.telegram.extraction import Extractor
from msu_hub_bot.telegram.files import DownloadableMedia, download
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.state import ReleasableEventIsolation, UpdateStateContext, release_state_isolation
from msu_hub_bot.media.sticker_media import MAX_INPUT_BYTES, StickerMediaError, prepare_media, prepare_custom_emoji
from msu_hub_bot.telegram.sticker_sets import StickerSetClient, UploadedSticker, UploadMetadata, sticker_error

sticker_set_name_template = "with_love_for_{id}_by_msu_hub_bot"
# Separate animated packs predate mixed-format sets; only /sd still edits them.
sticker_set_name_template_a = "with_love_for_{id}a_by_msu_hub_bot"
trimmed_sticker_notice = "✂️ Для стикера использованы только первые 7 секунд."
full_pack_notice = "🤷🏻‍♂️ Стикерпак заполнен: в нём уже 120 стикеров. Удалите ненужный командой /sd ответом на него."


class StickerStates(StatesGroup):
    sticker_set_name = State()


class ChatStickerDraft(BaseModel):
    """A pending pack title for /s (personal pack) or /sc (chat pack)."""

    mixed_sticker: InputSticker
    sticker_upload: UploadMetadata
    sticker_set_name: str
    sticker_chat_id: int
    sticker_user_id: int
    sticker_origin_message_id: int
    sticker_trimmed: bool = False
    sticker_personal: bool = False


# A title retry may overlap /cancel or a different conversation in the same topic.
_pending_saves: set[tuple[StorageKey, int]] = set()


async def choose_emojis(
    metadata: StickerMetadata,
    source: DownloadableMedia,
    bot: Bot,
    cpu_executor: TPExecutor,
    sticker_emoji: StickerEmojiPicker | None,
    clip: Callable[[], Awaitable[bytes]] | None = None,
) -> tuple[str, ...]:
    """Explicit emoji, then the source sticker's own, then Jev, then replied-text emoji or ✨."""
    if metadata.emojis_explicit:
        return metadata.emojis
    if isinstance(source, Sticker) and source.emoji:
        return (source.emoji,)
    if sticker_emoji is not None and (picked := await sticker_emoji.pick(source, bot, cpu_executor, clip)):
        return picked
    return metadata.emojis


class Stickers:
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
        # Personal title prompts from older releases kept only source file IDs.
        await state.clear()
        return await message.reply("🎈 Этот запрос на стикер устарел. Повторите /s ответом на картинку, GIF, видео или стикер.")

    @staticmethod
    def source_media(message: Message) -> tuple[DownloadableMedia | None, str | None]:
        for target in (message, message.reply_to_message):
            if target is None:
                continue
            if target.sticker:
                sticker = target.sticker
                if sticker.type == "mask":
                    raise StickerMediaError("Маски не подходят для обычного стикерпака. Пришлите стикер, картинку или эмодзи.")
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
    async def make_pack_sticker(
        cls,
        message: Message,
        meta: MetaInfo,
        state: FSMContext,
        name: str,
        bot: Bot,
        cpu_executor: TPExecutor,
        sticker_emoji: StickerEmojiPicker | None = None,
        *,
        personal: bool = False,
    ) -> Message | bool:
        if message.from_user is None:
            return True
        command = "/s" if personal else "/sc"
        try:
            metadata = parse_metadata(meta.text, meta.extract_text()[1])
            source, kind = cls.source_media(message)
            if source is None:
                source = await custom_emoji_source(message, bot)
                if source is not None:
                    kind = "animated" if source.is_animated else "video" if source.is_video else "static"
            if source is None:
                _, source = await Extractor.image(message, with_profile_photo=True)
                kind = "static"
            if source is None:
                return await message.reply(f"Ответьте {command} на картинку, GIF, видео, стикер или кастомный эмодзи.")
            if (source.file_size or 0) > MAX_INPUT_BYTES:
                raise StickerMediaError("Файл больше 20 МБ. Стикер не добавлен.")
            downloaded: list[bytes] = []

            async def load() -> bytes:
                # One download serves both emoji frames and conversion.
                if not downloaded:
                    file = await download(source, bot, max_bytes=MAX_INPUT_BYTES)
                    if file is None:
                        raise StickerMediaError("Не удалось скачать файл. Стикер не добавлен.")
                    with file:
                        downloaded.append(file.getvalue())
                return downloaded[0]

            emojis = await choose_emojis(metadata, source, bot, cpu_executor, sticker_emoji, load if kind == "video" else None)
            client = StickerSetClient(bot)
            trimmed = False
            if isinstance(source, Sticker) and reusable_sticker(source):
                uploaded = UploadedSticker(
                    source.file_id,
                    kind or "static",
                    emojis,
                    source.file_unique_id,
                    size=source.file_size,
                    keywords=metadata.keywords,
                    emojis_explicit=metadata.emojis_explicit,
                )
            else:
                is_custom = isinstance(source, Sticker) and source.type == "custom_emoji"
                if isinstance(source, Sticker) and source.type == "custom_emoji" and source.needs_repainting:
                    raise StickerMediaError(
                        "Этот эмодзи меняет цвет под тему Telegram. Пока не могу сохранить его цвета в обычном стикере. "
                        "Пришлите картинку, GIF или видео с нужным цветом."
                    )

                async def prepare_input() -> tuple[bytes, str]:
                    return await load(), kind or "static"

                prepared, timeouted = await cpu_executor.run_prepared(prepare_input, prepare_custom_emoji if is_custom else prepare_media)
                if timeouted:
                    raise StickerMediaError("Обработка заняла слишком много времени. Стикер не добавлен.")
                uploaded = await client.upload(
                    message.from_user.id,
                    prepared.payload,
                    prepared.kind,
                    emojis,
                    keywords=metadata.keywords,
                    emojis_explicit=metadata.emojis_explicit,
                )
                trimmed = prepared.trimmed
            if not await client.save(name, message.from_user.id, uploaded):
                draft = ChatStickerDraft(
                    mixed_sticker=uploaded.input_sticker(),
                    sticker_upload=UploadMetadata.model_validate(uploaded.metadata()),
                    sticker_set_name=name,
                    sticker_chat_id=message.chat.id,
                    sticker_user_id=message.from_user.id,
                    sticker_origin_message_id=message.message_id,
                    sticker_trimmed=trimmed,
                    sticker_personal=personal,
                )
                await state.set_data(draft.model_dump(mode="json"))
                await state.set_state(StickerStates.sticker_set_name)
                if personal:
                    return await message.reply(
                        "🎈 Для вас ещё нет стикерпака. Пришлите его название (1–64 символа) или /cancel. "
                        "Название видят все, и изменить его можно только через @Stickers."
                    )
                return await message.reply("🎈 Пришлите название стикерпака (1–64 символа) или /cancel.")
            return await cls.reply_saved_sticker(message, client, name, uploaded, trimmed=trimmed)
        except StickerMediaError as exc:
            return await message.reply(str(exc))
        except TelegramBadRequest as exc:
            if sticker_error(exc, "PEER_ID_INVALID"):
                return await message.reply(f"Сначала начните личный чат со мной, затем повторите {command}.")
            if sticker_error(exc, "STICKERS_TOO_MUCH"):
                return await message.reply(full_pack_notice)
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
        metadata_notice = ""
        if uploaded.emojis_explicit or uploaded.keywords is not None:
            if file_id is None or not await client.update_metadata(file_id, uploaded):
                metadata_notice = "Стикер сохранён. Обновление эмодзи и меток не подтверждено; их можно задать повторной командой."
        link = hlink("стикерпак", f"https://t.me/addstickers/{name}")
        if file_id is not None:
            try:
                reply = await message.reply_sticker(file_id)
            except TelegramBadRequest:
                pass
            else:
                notices = []
                if show_link:
                    notices.append(f"✨ Стикерпак: {link}. Управлять им можно через @Stickers.")
                if trimmed:
                    notices.append(trimmed_sticker_notice)
                if metadata_notice:
                    notices.append(metadata_notice)
                if notices:
                    await message.reply("\n".join(notices))
                return reply
        text = f"✨ Стикер добавлен в {link}. Откройте его в паке."
        if trimmed:
            text += "\n" + trimmed_sticker_notice
        if metadata_notice:
            text += "\n" + metadata_notice
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
        if not draft.sticker_personal and not await can_edit_chat_stickers(message, bot):
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


async def can_edit_chat_stickers(message: Message, bot: Bot) -> bool:
    if message.chat.type == ChatType.PRIVATE or not message.from_user or message.sender_chat:
        return False
    admins = await bot.get_chat_administrators(chat_id=message.chat.id)
    return message.from_user.id in (a.user.id for a in admins)


async def process_sticker(
    message: Message,
    meta: MetaInfo,
    state: FSMContext,
    bot: Bot,
    cpu_executor: TPExecutor,
    sticker_emoji: StickerEmojiPicker | None = None,
) -> Message | bool:
    if message.from_user is None or message.sender_chat:
        return True
    name = sticker_set_name_template.format(id=message.from_user.id)
    return await Stickers.make_pack_sticker(message, meta, state, name, bot, cpu_executor, sticker_emoji, personal=True)


async def process_sticker_chat(
    message: Message,
    meta: MetaInfo,
    state: FSMContext,
    bot: Bot,
    cpu_executor: TPExecutor,
    sticker_emoji: StickerEmojiPicker | None = None,
) -> Message | bool:
    if message.chat.type == ChatType.PRIVATE:
        return await message.reply("🤷🏻‍♂️ Эта команда только для чатов")
    if not await can_edit_chat_stickers(message, bot):
        return await message.reply("🤷🏻‍♂️ Стикерпак чата могут редактировать только его админы")
    name = sticker_set_name_template.format(id=abs(message.chat.id))
    return await Stickers.make_pack_sticker(message, meta, state, name, bot, cpu_executor, sticker_emoji)


async def process_sticker_delete(message: Message, bot: Bot) -> Message | bool:
    if message.from_user is None:
        return True
    if not (target := message.reply_to_message) or not (sticker := target.sticker):
        return await message.reply("Ответьте /sd на стикер из пака, который я создал для вас или этого чата.")
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

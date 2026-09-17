from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, MessageOriginChannel, MessageOriginChat, ReplyKeyboardMarkup
from aiogram.utils.markdown import hbold, hcode
from pydantic import BaseModel, Field

from common.db.base import BotRepository
from common.tg.runtime import gather_complete
from common.tg.state import UpdateStateContext, release_state_isolation
from common.tg.wrapper import BotWrapper


class MakePostStates(StatesGroup):
    destination = State()
    waiting = State()


class PostDraft(BaseModel):
    post_chat_id: int
    post_message_id: int
    dest_chat_ids: list[int] = Field(default_factory=list)


def _instructions(title: str, example_chat_id: int) -> str:
    return (
        f"{hbold(title)}\n\n"
        "Укажите где следует разместить пост, это можно сделать тремя способами:\n"
        f"1. Прислать юзернейм чата / канала, например: {hcode('@chat_msu')}\n"
        f"2. Прислать id чата / канала, например {hcode(str(example_chat_id or -1001234567890))}\n"
        "3. Если это канал, то перешлите мне сообщение из него\n\n"
        "Чтобы выйти из процесса рассылки — /cancel."
    )


class MakePost:
    @classmethod
    def keyboard(cls) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Запустить рассылку")],
                [KeyboardButton(text="Добавить целевой чат")],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
            selective=True,
        )

    @classmethod
    async def process(cls, message: Message, state: FSMContext, posting_main_chat_id: int) -> Message:
        reply_to = message.reply_to_message
        if not reply_to:
            return await message.reply("Используйте команду реплаем на пост, который хотите разослать")
        draft = PostDraft(post_chat_id=reply_to.chat.id, post_message_id=reply_to.message_id)
        await state.set_data(draft.model_dump())
        await state.set_state(MakePostStates.destination)
        return await message.reply(_instructions("Создание рассылки", posting_main_chat_id))

    @classmethod
    async def process_waiting(
        cls,
        message: Message,
        state: FSMContext,
        bot: BotWrapper,
        state_context: UpdateStateContext,
        posting_main_chat_id: int,
    ) -> Message:
        if message.text == "Запустить рассылку":
            return await cls.process_run(message, state, bot, state_context)
        if message.text == "Добавить целевой чат":
            await state.set_state(MakePostStates.destination)
            return await message.reply(_instructions("Добавление целевого чата", posting_main_chat_id))
        return await message.reply("Выберите одно из предложенных действий или жмите /cancel")

    @classmethod
    async def process_destination(cls, message: Message, state: FSMContext, bot: BotWrapper) -> Message:
        origin = message.forward_origin
        if isinstance(origin, (MessageOriginChannel, MessageOriginChat)):
            destination: int | str = origin.chat.id if isinstance(origin, MessageOriginChannel) else origin.sender_chat.id
        elif message.text:
            destination = message.text
        else:
            return await message.reply("Ожидаю текстовое сообщение или /cancel")
        try:
            chat = await bot.get_chat(destination)
        except TelegramAPIError:
            return await message.reply("Что-то пошло не так: возможно у меня нет доступа к этому чату. Пришлите другой или жмите /cancel.")
        draft = PostDraft.model_validate(await state.get_data())
        draft.dest_chat_ids.append(chat.id)
        await state.set_data(draft.model_dump())
        await state.set_state(MakePostStates.waiting)
        return await message.reply(
            f"Отлично, чат {hcode(chat.full_name)} добавлен в рассылку, выберите следующее действие.",
            reply_markup=cls.keyboard(),
        )

    @classmethod
    async def process_run(
        cls,
        message: Message,
        state: FSMContext,
        bot: BotWrapper,
        state_context: UpdateStateContext,
    ) -> Message:
        draft = PostDraft.model_validate(await state.get_data())
        await state.clear()
        # The draft is consumed before delivery; /cancel does not cancel a running broadcast.
        release_state_isolation(state_context)
        for chat_id in draft.dest_chat_ids:
            try:
                await bot.copy_message(chat_id, draft.post_chat_id, draft.post_message_id)
            except TelegramAPIError:
                await message.answer(f"Не удалось отправить пост в чат {hcode(str(chat_id))}. Пропускаю его и продолжаю рассылку.")
        return await message.answer("Рассылка завершена!")


async def _destinations(db: BotRepository, posting_tb_chat_id: int) -> list[int]:
    chats = await db.list_directory()
    return [
        chat.chat_id for chat in chats if (chat.members or 0) >= 55 and chat.section != "channel" and chat.chat_id != posting_tb_chat_id
    ]


async def process_post_all(
    message: Message,
    bot: BotWrapper,
    db: BotRepository,
    posting_tb_chat_id: int,
) -> list[Message] | None:
    if not (post_message := message.reply_to_message):
        return None
    chat_ids = await _destinations(db, posting_tb_chat_id)
    return await gather_complete(*(bot(post_message.send_copy(chat_id, disable_notification=True)) for chat_id in chat_ids))


async def process_post_forward_all(
    message: Message,
    bot: BotWrapper,
    db: BotRepository,
    posting_tb_chat_id: int,
) -> list[Message] | None:
    if not (post_message := message.reply_to_message):
        return None
    chat_ids = await _destinations(db, posting_tb_chat_id)
    return await gather_complete(
        *(bot.forward_message(chat_id, post_message.chat.id, post_message.message_id, disable_notification=True) for chat_id in chat_ids)
    )

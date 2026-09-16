from contextlib import suppress
from collections.abc import Awaitable, Callable
import re

import cachetools
from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, MessageEntityType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.markdown import hpre, hbold, hcode, hitalic
from pydantic import BaseModel

from common.tg.callbacks import CallbackCommandBase
from common.tg.filters import MetaCommand, MetaInfo
from common.tg.state import UpdateStateContext, release_state_isolation
from common.tg.files import download_text
from hub_bot.utils.jdoodle import LANGUAGES, JDoodleError, ManyJDoodle

CompilerHandler = Callable[..., Awaitable[Message | bool]]


def _register(router: Router, handler: CompilerHandler, *aliases: str) -> None:
    for observer in (router.message, router.edited_message):
        observer.register(handler, StateFilter(None), MetaCommand(*aliases), flags={"fsm_release": True})


def register_code_submitters(router: Router) -> Router:
    # Only the two documented underscore hashtags gain priority. The short
    # #pys/#pythons aliases keep their original position among compiler routes.
    explicit_stdin = re.compile(r"#\b(?:py_stdin|python_stdin)(?:_[\w\d]*)*\b", re.IGNORECASE)
    for observer in (router.message, router.edited_message):
        observer.register(
            ProgCompiler.process_stdin_builder("python3"),
            StateFilter(None),
            F.text.regexp(explicit_stdin) | F.caption.regexp(explicit_stdin),
            MetaCommand("py_stdin", "python_stdin"),
            flags={"fsm_release": True},
        )
    for lang in LANGUAGES:
        _register(router, ProgCompiler.process_builder(lang), lang)
    _register(router, ProgCompiler.process_builder("python2"), "py2")
    _register(router, ProgCompiler.process_builder("python3"), "py", "python")
    _register(router, ProgCompiler.process_builder("nodejs"), "js", "javascript")
    return router


def register_code_submitters_with_stdin(router: Router) -> Router:
    def with_stdin(s: str) -> tuple[str, str]:
        return s + "_stdin", s + "s"

    for lang in LANGUAGES:
        _register(router, ProgCompiler.process_stdin_builder(lang), *with_stdin(lang))
    _register(router, ProgCompiler.process_stdin_builder("python2"), *with_stdin("py2"))
    _register(router, ProgCompiler.process_stdin_builder("python3"), *with_stdin("py"), *with_stdin("python"))
    _register(router, ProgCompiler.process_stdin_builder("nodejs"), *with_stdin("js"), *with_stdin("javascript"))
    return router


async def process_code(message: Message) -> Message:
    text = f"{hbold('Доступные языки')}:\n\n"
    for lang, (name, versions) in LANGUAGES.items():
        text += f"— {hbold(name)} | #{lang}, {versions[-1][0]}\n"
    text += f"\n"

    text += f"{hbold('Некоторые синонимы')}:\n"
    text += f"— Python 2 | #py2\n"
    text += f"— Python 3 | #py, #python\n"
    text += f"— NodeJS | #js, #javascript\n"
    text += f"\n"

    text += (
        f"{hbold('Примечание')}: Чтобы получить возможность задать пользовательский ввод, "
        f"к команде нужно сделать приписку {hcode('_stdin')} или {hcode('s')}. "
        f"Например, #py_stdin или #pys.\n\n"
    )

    text += (
        f"{hbold('Примечание')}: Вы можете редактировать сообщение с кодом, "
        f"бот автоматически обновит результат на запуске с исправленным кодом.\n\n"
    )

    text += (
        f"{hbold('Как пользоваться')}: вместе с кодом программы нужно прислать хештег, "
        f"чтобы бот понял, что сообщение нужно исполнить на указанном языке. "
        f"Хештег будет вырезан из текста и не повлияет на расчёт.\n\n"
    )
    return await message.reply(text)


async def code_submit(jdoodle: ManyJDoodle, source_code: str, stdin: str = "", lang: str = "python3") -> str | None:
    with suppress(JDoodleError):
        result = await jdoodle.instance.request_and_parse(source_code, stdin, lang)
        return result
    return None


class ProgStates(StatesGroup):
    stdin = State()


class ProgCallback(CallbackData, prefix="prog"):
    action: str


class StdinDraft(BaseModel):
    chat_id: int
    inform_message_id: int
    prog_lang: str
    prog_code: str


class ProgCompiler(CallbackCommandBase):
    callback_data = ProgCallback
    replies: cachetools.LRUCache[tuple[int, int], int] = cachetools.LRUCache(maxsize=128)

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⌨️ Задать ввод", callback_data=ProgCallback(action="input").pack()),
                    InlineKeyboardButton(text="✖️ Отмена ввода", callback_data=ProgCallback(action="cancel").pack()),
                ]
            ]
        )

    @classmethod
    def process_builder(cls, lang: str) -> CompilerHandler:
        async def process(message: Message, meta: MetaInfo, bot: Bot, jdoodle: ManyJDoodle) -> Message | bool:
            target, text = await meta.extract_text_with_doc_plain()
            if not text:
                return True

            await bot.send_chat_action(
                chat_id=message.chat.id,
                action=ChatAction.TYPING,
                message_thread_id=message.message_thread_id if message.is_topic_message else None,
            )
            header = hbold(lang) + " | " + hbold(LANGUAGES[lang][1][-1][0]) + "\n\n"

            advance = None
            if message_id := cls.replies.get(cls.cache_key(target)):
                with suppress(TelegramBadRequest):
                    edited = await bot.edit_message_text(header + hitalic("🔄 Ожидание..."), chat_id=message.chat.id, message_id=message_id)
                    if isinstance(edited, Message):
                        advance = edited

            if advance is None:
                advance = await target.reply(header + hitalic("🔄 Ожидание..."))
                cls.replies[cls.cache_key(target)] = advance.message_id

            result = await code_submit(jdoodle, text, lang=lang)
            if result:
                return await advance.edit_text(header + hpre(result))

            return await advance.edit_text(hcode("🤷🏻‍♂️ Произошла какая-то ошибка"))

        return process

    @classmethod
    def process_stdin_builder(cls, lang: str) -> CompilerHandler:
        async def process_code_submit_with_stdin(message: Message, meta: MetaInfo, bot: Bot) -> Message | bool:
            return await ProgCompiler.process_stdin(message, meta, bot, lang)

        return process_code_submit_with_stdin

    @classmethod
    async def process_stdin(cls, message: Message, meta: MetaInfo, bot: Bot, lang: str) -> Message | bool:
        target, text, doc = meta.extract_text_with_doc()
        if text:
            text = hpre(text)
        else:
            if not doc:
                return True
            text = hcode(doc.file_name or "code.txt")

        text = f"{hbold(lang)} | {hbold(LANGUAGES[lang][1][-1][0])} | with stdin\n\n{text}"

        result = None
        if message_id := cls.replies.get(cls.cache_key(target)):
            with suppress(TelegramBadRequest):
                edited = await bot.edit_message_text(text, chat_id=message.chat.id, message_id=message_id, reply_markup=cls.keyboard())
                if isinstance(edited, Message):
                    result = edited

        if result is None:
            result = await target.reply(text, reply_markup=cls.keyboard())
            cls.replies[cls.cache_key(target)] = result.message_id

        return result

    @classmethod
    async def process_stdin_cb(cls, query: CallbackQuery, state: FSMContext, callback_data: ProgCallback, bot: Bot) -> Message | bool:
        m = query.message
        if not isinstance(m, Message):
            return await query.answer()
        action = callback_data.action

        if action == "cancel":
            if await state.get_state() != ProgStates.stdin.state:
                return await query.answer("💁🏻‍♂️ Вы не в процессе ввода", cache_time=1)

            data = StdinDraft.model_validate(await state.get_data())
            with suppress(TelegramBadRequest):
                await bot.delete_message(chat_id=data.chat_id, message_id=data.inform_message_id)
            await state.clear()
            return await query.answer("🆗 Ввод отменён", cache_time=3)

        if await state.get_state() == ProgStates.stdin.state:
            return await query.answer("🔄 Бот уже ждёт твой ввод", cache_time=3, show_alert=True)

        await query.answer("⬇️ Теперь ожидаю ввод", cache_time=3)

        codes = [e.extract_from(m.text or "") for e in m.entities or [] if e.type == MessageEntityType.PRE]
        code: str | None
        if codes:
            code = codes[0]
        else:
            if m.reply_to_message and m.reply_to_message.document:
                code = await download_text(m.reply_to_message.document.file_id, bot)
            else:
                code = None
        languages = [e.extract_from(m.text or "") for e in m.entities or [] if e.type == MessageEntityType.BOLD]
        if code is None or not languages or languages[0] not in LANGUAGES:
            return await m.edit_text(m.html_text + "\n\n⚠️ Сообщение с исходным кодом удалено")
        lang = languages[0]

        reply = await m.reply(f"{query.from_user.mention_html()}, ожидаю ввод ⬇️, или /cancel")
        data = StdinDraft(chat_id=reply.chat.id, inform_message_id=reply.message_id, prog_lang=lang, prog_code=code)
        await state.set_data(data.model_dump())
        await state.set_state(ProgStates.stdin)

        return True

    @classmethod
    async def process_stdin_run(
        cls, message: Message, state: FSMContext, bot: Bot, jdoodle: ManyJDoodle, state_context: UpdateStateContext
    ) -> Message | bool:
        await bot.send_chat_action(
            chat_id=message.chat.id,
            action=ChatAction.TYPING,
            message_thread_id=message.message_thread_id if message.is_topic_message else None,
        )
        data = StdinDraft.model_validate(await state.get_data())
        with suppress(TelegramBadRequest):
            await bot.delete_message(chat_id=data.chat_id, message_id=data.inform_message_id)
        lang, code = data.prog_lang, data.prog_code
        await state.clear()
        release_state_isolation(state_context)

        header = hbold(lang) + " | " + hbold(LANGUAGES[lang][1][-1][0]) + "\n\n"
        advance = await message.reply(header + hitalic("🔄 Ожидание..."))

        result = await code_submit(jdoodle, code, stdin=message.text or message.caption or "", lang=lang)
        if result:
            return await advance.edit_text(header + hpre(result))

        return await advance.edit_text(hcode("🤷🏻‍♂️ Произошла какая-то ошибка"))

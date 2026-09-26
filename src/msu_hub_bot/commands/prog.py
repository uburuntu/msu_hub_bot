from contextlib import suppress
from collections.abc import Awaitable, Callable
import asyncio
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
from aiogram.utils.markdown import hbold, hcode
from aiogram.utils.formatting import Bold, Code, Italic, Pre, Text, TextLink
from pydantic import BaseModel
from teleforge.delivery import DeliveryError, DeliveryProgress, complete_response, edit_response, send_response
from teleforge.formatting import ResponseError

from msu_hub_bot.telegram.callbacks import CallbackCommandBase
from msu_hub_bot.telegram.filters import MetaCommand, MetaInfo
from msu_hub_bot.telegram.state import UpdateStateContext, release_state_isolation
from msu_hub_bot.telegram.files import download_text
from msu_hub_bot.providers.jdoodle import LANGUAGES, JDoodleError, ManyJDoodle
from msu_hub_bot.telemetry import record_handled_failure

CompilerHandler = Callable[..., Awaitable[Message | bool]]


def _register(router: Router, handler: CompilerHandler, handler_key: str, *aliases: str) -> None:
    for observer in (router.message, router.edited_message):
        observer.register(handler, StateFilter(None), MetaCommand(*aliases), flags={"fsm_release": True, "handler_key": handler_key})


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
            flags={"fsm_release": True, "handler_key": "compile.stdin_prompt.python3"},
        )
    for lang in LANGUAGES:
        _register(router, ProgCompiler.process_builder(lang), f"compile.{lang}", lang)
    _register(router, ProgCompiler.process_builder("python2"), "compile.python2", "py2")
    _register(router, ProgCompiler.process_builder("python3"), "compile.python3", "py", "python")
    _register(router, ProgCompiler.process_builder("nodejs"), "compile.nodejs", "js", "javascript")
    return router


def register_code_submitters_with_stdin(router: Router) -> Router:
    def with_stdin(s: str) -> tuple[str, str]:
        return s + "_stdin", s + "s"

    for lang in LANGUAGES:
        _register(router, ProgCompiler.process_stdin_builder(lang), f"compile.stdin_prompt.{lang}", *with_stdin(lang))
    _register(router, ProgCompiler.process_stdin_builder("python2"), "compile.stdin_prompt.python2", *with_stdin("py2"))
    _register(
        router, ProgCompiler.process_stdin_builder("python3"), "compile.stdin_prompt.python3", *with_stdin("py"), *with_stdin("python")
    )
    _register(
        router, ProgCompiler.process_stdin_builder("nodejs"), "compile.stdin_prompt.nodejs", *with_stdin("js"), *with_stdin("javascript")
    )
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


def _header(lang: str) -> Text:
    return Text(Bold(lang), " | ", Bold(LANGUAGES[lang][1][-1][0]), "\n\n")


async def _run_code(
    jdoodle: ManyJDoodle, code: str, lang: str, status: Message, source: Message, bot: Bot, *, stdin: str = ""
) -> Message | bool:
    progress = DeliveryProgress()
    try:
        output = await code_submit(jdoodle, code, stdin=stdin, lang=lang)
        content = Text(_header(lang), Pre(output)) if output else Code("🤷🏻‍♂️ Произошла какая-то ошибка")
        completed = await complete_response(
            bot, status, content, overflow_to=source, overflow_notice="Готово — полный результат в файле.", progress=progress
        )
    except asyncio.CancelledError:
        if progress.phase != "complete" and not progress.uncertain:
            with suppress(DeliveryError, ResponseError):
                await edit_response(bot, status, "Выполнение отменено.")
        raise
    except (DeliveryError, ResponseError) as error:
        record_handled_failure(error)
        notice = "Не удалось подтвердить отправку. Результат мог уже прийти." if progress.uncertain else "Не удалось отправить результат."
        with suppress(DeliveryError, ResponseError):
            if progress.uncertain:
                guidance = await send_response(bot, source, notice, fixed=True)
                assert isinstance(guidance, Message)
                return guidance
            return await edit_response(bot, status, notice)
        raise
    if completed.status_error is not None:
        record_handled_failure(completed.status_error)
    return completed.result


class ProgStates(StatesGroup):
    stdin = State()


class ProgCallback(CallbackData, prefix="prog"):
    action: str


class StdinDraft(BaseModel):
    chat_id: int
    inform_message_id: int
    prog_lang: str
    prog_code: str


async def stdin_source(message: Message, bot: Bot) -> tuple[str, str] | None:
    """Resolve a compact preview's inline program or original source document."""
    codes = [entity.extract_from(message.text or "") for entity in message.entities or () if entity.type == MessageEntityType.PRE]
    code: str | None
    if codes:
        code = codes[0]
    elif message.reply_to_message and message.reply_to_message.document:
        code = await download_text(message.reply_to_message.document.file_id, bot)
    else:
        code = None
    languages = [entity.extract_from(message.text or "") for entity in message.entities or () if entity.type == MessageEntityType.BOLD]
    if code is None or not languages or languages[0] not in LANGUAGES:
        return None
    return languages[0], code


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
            waiting = Text(_header(lang), Italic("🔄 Ожидание..."))

            advance = None
            if message_id := cls.replies.get(cls.cache_key(target)):
                with suppress(TelegramBadRequest):
                    edited = await bot.edit_message_text(**waiting.as_kwargs(), chat_id=message.chat.id, message_id=message_id)
                    if isinstance(edited, Message):
                        advance = edited

            if advance is None:
                sent = await send_response(bot, target, waiting, fixed=True)
                assert isinstance(sent, Message)
                advance = sent
                cls.replies[cls.cache_key(target)] = advance.message_id

            return await _run_code(jdoodle, text, lang, advance, target, bot)

        return process

    @classmethod
    def process_stdin_builder(cls, lang: str) -> CompilerHandler:
        async def process_code_submit_with_stdin(message: Message, meta: MetaInfo, bot: Bot) -> Message | bool:
            return await ProgCompiler.process_stdin(message, meta, bot, lang)

        return process_code_submit_with_stdin

    @classmethod
    async def process_stdin(cls, message: Message, meta: MetaInfo, bot: Bot, lang: str) -> Message | bool:
        target, text, doc = meta.extract_text_with_doc()
        content: Text
        if text:
            content = Pre(text)
        else:
            if not doc:
                return True
            content = Code(doc.file_name or "code.txt")

        preview = Text(Bold(lang), " | ", Bold(LANGUAGES[lang][1][-1][0]), " | with stdin\n\n", content)

        result = None
        if message_id := cls.replies.get(cls.cache_key(target)):
            with suppress(TelegramBadRequest):
                edited = await bot.edit_message_text(
                    **preview.as_kwargs(), chat_id=message.chat.id, message_id=message_id, reply_markup=cls.keyboard()
                )
                if isinstance(edited, Message):
                    result = edited

        if result is None:
            sent = await send_response(bot, target, preview, reply_markup=cls.keyboard(), fixed=True)
            assert isinstance(sent, Message)
            result = sent
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

        source = await stdin_source(m, bot)
        if source is None:
            return await m.edit_text(m.html_text + "\n\n⚠️ Сообщение с исходным кодом удалено")
        lang, code = source

        reply = await send_response(
            bot,
            m,
            Text(TextLink(query.from_user.full_name, url=f"tg://user?id={query.from_user.id}"), ", ожидаю ввод ⬇️, или /cancel"),
            fixed=True,
        )
        assert isinstance(reply, Message)
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

        advance = await send_response(bot, message, Text(_header(lang), Italic("🔄 Ожидание...")), fixed=True)
        assert isinstance(advance, Message)
        return await _run_code(jdoodle, code, lang, advance, message, bot, stdin=message.text or message.caption or "")

"""Keep the bot's slash and hashtag grammar at its application boundary."""

from collections.abc import Callable
from typing import Any

from aiogram import Bot
from aiogram.types import Message
from teleforge import command as declare_command
from teleforge.inputs import InputError

from msu_hub_bot.telegram.filters import MetaCommand, MetaInfo


class HubCommand(MetaCommand):
    async def __call__(self, message: Message, bot: Bot) -> bool | dict[str, Any]:
        result = await super().__call__(message, bot)
        if not isinstance(result, dict):
            return result
        meta = result["meta"]
        assert isinstance(meta, MetaInfo)
        if meta.hashtag:
            # Underscores carry arguments; surrounding text remains a separate input.
            return {**result, "_teleforge_tail": " ".join(meta.arguments), "_teleforge_text": meta.text}
        return {**result, "_teleforge_tail": meta.text}


def command[Handler: Callable[..., Any]](*names: str, **options: Any) -> Callable[[Handler], Handler]:
    """Declare one alias list for both inspection and the bot's native grammar."""
    return declare_command(*names, filter=HubCommand(*names), **options)


def format_input_error(issue: InputError) -> str:
    """The host supplies product copy; TeleForge owns validation and safe issue codes."""
    messages = {
        "attachment-too-large": "Файл слишком большой. Попробуй прислать поменьше.",
        "text-too-large": "Текста слишком много. Попробуй сократить его.",
        "text-encoding": "Пришли текстовый файл в кодировке UTF-8.",
        "image-dimensions": "Картинка слишком большая. Уменьши её разрешение и попробуй ещё раз.",
        "image-decode": "Не удалось прочитать картинку. Попробуй прислать её ещё раз.",
        "callback-invalid": "Эта кнопка устарела. Открой команду ещё раз.",
        "argument-invalid": "Не удалось разобрать аргумент. Проверь команду и попробуй ещё раз.",
        "argument-missing": "Добавь аргумент к команде — пример есть в /help.",
        "text-missing": "Добавь текст после команды или ответь ею на сообщение с текстом.",
        "text-invalid": "Текст не подходит для команды. Проверь формат — пример есть в /help.",
        "media-missing": "Прикрепи подходящий файл или ответь командой на сообщение с ним.",
        "media-type": "Этот файл не подходит для команды. Попробуй другой формат.",
    }
    if issue.code == "text-too-long":
        return f"Текст слишком длинный. Попробуй уложиться в {issue.params['limit']} символов."
    return messages[issue.code]

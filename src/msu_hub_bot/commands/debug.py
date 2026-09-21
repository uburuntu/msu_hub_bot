import json
from datetime import datetime

from aiogram import html
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.markdown import hpre

from msu_hub_bot.telegram.constants import TELEGRAM_MESSAGE_MAX_LEN
from msu_hub_bot.logger import LoggerBuilder
from msu_hub_bot.telegram.deletions import MessageDeletions
from msu_hub_bot.telegram.utils import command_arguments, send_super_reply
from msu_hub_bot.utils import parse_int
from msu_hub_bot.redaction import redact, redact_json


def _json_default(value: object) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp())
    raise TypeError("Unsupported diagnostic JSON value")


async def process_json(message: Message) -> Message:
    """Return complete identifiers and valid JSON; larger dumps become files."""
    target_message = message.reply_to_message or message
    target = target_message.model_dump(mode="python", by_alias=True, exclude_none=True, exclude_unset=True)
    text = json.dumps(redact_json(target), ensure_ascii=False, indent=True, default=_json_default)
    if len(text.encode("utf-16-le")) // 2 > TELEGRAM_MESSAGE_MAX_LEN:
        document = BufferedInputFile(text.encode("utf-8"), filename=f"message-{target_message.message_id}.json")
        return await target_message.reply_document(document, disable_notification=True)
    return await target_message.reply(hpre(text), disable_notification=True)


async def process_logs(message: Message) -> Message | bool | None:
    if not LoggerBuilder.default_filename:
        return True
    arguments = command_arguments(message)
    lines = int(arguments) if arguments.isdigit() else 100
    with open(LoggerBuilder.default_filename, encoding="utf-8") as file:
        tail = file.readlines()[-lines:]
    text = html.quote(redact("".join(tail)))
    return await send_super_reply(message, text, text_postprocess=hpre)


async def process_delete_after(message: Message, deletions: MessageDeletions) -> bool:
    if not message.reply_to_message:
        return True
    args = command_arguments(message).split()
    after = (parse_int(args[0], 0, 3, 10 * 24 * 60 * 60) or 0) if args else 0
    return await deletions.mark_message_to_delete(message.reply_to_message, after=after)

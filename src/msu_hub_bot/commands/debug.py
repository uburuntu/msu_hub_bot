import json
from datetime import datetime

from aiogram import html
from aiogram.types import Message
from aiogram.utils.markdown import hpre

from msu_hub_bot.telegram.constants import TELEGRAM_MESSAGE_MAX_LEN
from msu_hub_bot.logger import LoggerBuilder
from msu_hub_bot.telegram.storage import RedisStorage
from msu_hub_bot.telegram.utils import command_arguments, send_super_reply
from msu_hub_bot.utils import parse_int
from msu_hub_bot.redaction import redact


def _json_default(value: object) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp())
    raise TypeError("Unsupported diagnostic JSON value")


async def process_json(message: Message) -> Message:
    target_message = message.reply_to_message or message
    target = target_message.model_dump(mode="python", by_alias=True, exclude_none=True)
    cut_length = TELEGRAM_MESSAGE_MAX_LEN // 2
    if text_part := target.get("text"):
        if len(text_part) > cut_length:
            target["text"] = text_part[:cut_length] + "..."
    if "pinned_message" in target.get("chat", {}):
        target["chat"]["pinned_message"] = "{ ... }"
    text = hpre(redact(json.dumps(target, ensure_ascii=False, indent=True, default=_json_default))[:TELEGRAM_MESSAGE_MAX_LEN])
    return await target_message.reply(text, disable_notification=True)


async def process_logs(message: Message) -> Message | bool | None:
    if not LoggerBuilder.default_filename:
        return True
    arguments = command_arguments(message)
    lines = int(arguments) if arguments.isdigit() else 100
    with open(LoggerBuilder.default_filename, encoding="utf-8") as file:
        tail = file.readlines()[-lines:]
    text = html.quote(redact("".join(tail)))
    return await send_super_reply(message, text, text_postprocess=hpre)


async def process_delete_after(message: Message, redis: RedisStorage) -> bool:
    if not message.reply_to_message:
        return True
    args = command_arguments(message).split()
    after = (parse_int(args[0], 0, 3, 10 * 24 * 60 * 60) or 0) if args else 0
    return await redis.mark_message_to_delete(message.reply_to_message, after=after)

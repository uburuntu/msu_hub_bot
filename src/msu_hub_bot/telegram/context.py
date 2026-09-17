"""Validated access to the bot attached to a Telegram object."""

from aiogram import Bot
from aiogram.client.context_controller import BotContextController


def bot_for(value: BotContextController) -> Bot:
    bot = value.bot
    if bot is None:
        raise RuntimeError("Telegram object has no bot context")
    return bot

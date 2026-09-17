"""Reusable keyboards with stable callback wire formats."""

from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


class RateCallback(CallbackData, prefix="rate"):
    is_up: Literal["+", "-"]


def rate_keyboard(up: int = 0, down: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"{up} 👍🏻", callback_data=RateCallback(is_up="+").pack()),
                InlineKeyboardButton(text=f"{down} 👎🏻", callback_data=RateCallback(is_up="-").pack()),
            ]
        ]
    )

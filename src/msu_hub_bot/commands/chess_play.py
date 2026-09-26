"""Public chess matches and the bot-wide Elo leaderboard."""

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from aiogram.utils.formatting import Bold, Text

from msu_hub_bot.commands.chess_play_view import PlayCallback
from msu_hub_bot.commands.quiz_view import user_label
from msu_hub_bot.games.chess_play.models import GameError
from msu_hub_bot.games.chess_play.service import ChessMatchService
from msu_hub_bot.telegram.context import bot_for

logger = logging.getLogger(__name__)


class ChessPlay:
    callback_data = PlayCallback

    @staticmethod
    async def process(message: Message, chess_matches: ChessMatchService) -> Message | None:
        try:
            return await chess_matches.start(message)
        except GameError as error:
            answer = str(error)
        except Exception:
            logger.exception("Chess match unavailable")
            answer = "Не удалось открыть партию. Попробуй чуть позже."
        async with asyncio.timeout(15):
            return await bot_for(message)(message.reply(answer), request_timeout=15)

    @staticmethod
    async def callback(query: CallbackQuery, callback_data: PlayCallback, chess_matches: ChessMatchService) -> None:
        await chess_matches.callback(query, callback_data)


class RatingCallback(CallbackData, prefix="chrate"):
    page: int


class ChessRating:
    callback_data = RatingCallback

    @staticmethod
    async def _view(chess_matches: ChessMatchService, user: User, page: int = 0) -> tuple[Text, InlineKeyboardMarkup | None]:
        async with asyncio.timeout(10):
            board, person = await chess_matches.rating(user.id, page=max(0, page))
        rows: list[Text | str] = [Bold("♟ Общий шахматный рейтинг"), "\nЗа всё время · старт 800 · Elo\n"]
        for index, player in enumerate(board.players, board.page * 10 + 1):
            rows.append(Text(f"\n{index}. ", user_label(player.user_id, player.name, player.username), f" — {player.rating}"))
        if not board.players:
            rows.append("\nПока никто не сыграл. Начни с /chess_play.")
        rows.append(
            Text(
                "\n\n",
                user_label(user.id, user.full_name, user.username),
                f": {person.rating}",
                f" · место {person.rank}" if person.rank is not None else " · ещё нет партий",
            )
        )
        markup = None
        if board.pages > 1:
            rows.append(f"\nСтраница {board.page + 1}/{board.pages}")
            buttons = [
                InlineKeyboardButton(text=label, callback_data=RatingCallback(page=number).pack())
                for label, number in (("‹", board.page - 1), ("›", board.page + 1))
                if 0 <= number < board.pages
            ]
            markup = InlineKeyboardMarkup(inline_keyboard=[buttons])
        if board.total > board.pages * 10:
            rows.append(f"\nВ списке — первые {board.pages * 10:,} мест; твоё место учитывает всех.".replace(",", " "))
        return Text(*rows), markup

    @classmethod
    async def process(cls, message: Message, chess_matches: ChessMatchService) -> Message | None:
        if message.from_user is None:
            return None
        try:
            text, markup = await cls._view(chess_matches, message.from_user)
        except Exception:
            logger.exception("Chess ratings unavailable")
            text, markup = Text("Рейтинг сейчас недоступен. Попробуй чуть позже."), None
        async with asyncio.timeout(15):
            return await bot_for(message)(
                message.reply(**text.as_kwargs(), reply_markup=markup, disable_notification=True), request_timeout=15
            )

    @classmethod
    async def callback(cls, query: CallbackQuery, callback_data: RatingCallback, chess_matches: ChessMatchService) -> None:
        answer = ""
        try:
            if not isinstance(query.message, Message):
                answer = "Сообщение недоступно. Открой /chess_rating заново."
                return
            text, markup = await cls._view(chess_matches, query.from_user, callback_data.page)
            async with asyncio.timeout(15):
                await bot_for(query)(query.message.edit_text(**text.as_kwargs(), reply_markup=markup), request_timeout=15)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                logger.exception("Chess rating page unavailable")
                answer = "Не удалось обновить рейтинг. Открой /chess_rating заново."
        except Exception:
            logger.exception("Chess ratings unavailable")
            answer = "Рейтинг сейчас недоступен. Попробуй чуть позже."
        finally:
            async with asyncio.timeout(15):
                await bot_for(query)(query.answer(answer), request_timeout=15)

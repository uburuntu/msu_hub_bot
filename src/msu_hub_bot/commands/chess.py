"""Chat chess puzzles with hidden votes and a daily, chat-local leaderboard."""

import asyncio
from datetime import date

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message

from msu_hub_bot.games.quiz import QuizService
from msu_hub_bot.games import scores
from msu_hub_bot.games.scores import DAY_ZONE as DAY_ZONE, today as today
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.storage import RedisStorage


def score_key(chat_id: int, day: date | None = None) -> str:
    return scores.score_key("chess", chat_id, day or today())


async def save_scores(
    chat_id: int,
    players: list[tuple[int, str, str | None, int]],
    redis: RedisStorage,
    day: date | None = None,
    *,
    round_token: str,
) -> None:
    await scores.save_scores("chess", chat_id, players, redis, day or today(), round_token=round_token)


class ChessCallback(CallbackData, prefix="chess"):
    round: str
    choice: str


class Chess:
    callback_data = ChessCallback

    @staticmethod
    async def process(message: Message, quiz: QuizService) -> Message | None:
        return await quiz.start("chess", message)

    @staticmethod
    async def process_cb(query: CallbackQuery, callback_data: ChessCallback, quiz: QuizService) -> bool | None:
        if not isinstance(query.message, Message):
            async with asyncio.timeout(15):
                return await bot_for(query)(query.answer("Этот раунд недоступен."), request_timeout=15)
        return await quiz.callback("chess", query, callback_data.round, callback_data.choice)

    @staticmethod
    async def top(message: Message, redis: RedisStorage) -> Message:
        try:
            async with asyncio.timeout(5):
                body = await scores.ranking("chess", message, redis, today())
            text, entities = body.render()
            method = message.reply(text, entities=entities, parse_mode=None)
        except Exception:
            method = message.reply("Рейтинг сейчас недоступен.")
        async with asyncio.timeout(15):
            return await bot_for(message)(method, request_timeout=15)

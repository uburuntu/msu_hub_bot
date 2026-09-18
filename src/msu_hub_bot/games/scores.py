"""Daily Redis rankings with atomic, replay-safe round settlement."""

from collections.abc import Awaitable
from datetime import date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from aiogram.types import Message
from aiogram.utils.formatting import Text

from msu_hub_bot.commands.quiz_view import user_label
from msu_hub_bot.telegram.storage import RedisStorage

DAY_ZONE = ZoneInfo("Europe/Moscow")


class ScoreWindowExpired(RuntimeError):
    """Redis's clock has passed the fixed daily settlement deadline."""


def today() -> date:
    return datetime.now(DAY_ZONE).date()


def score_key(feature: str, chat_id: int, day: date | None = None) -> str:
    return f"msu_hub:{feature}:{chat_id}:{(day or today()).isoformat()}:scores"


def score_expiry(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=2), time.min, DAY_ZONE)


_SAVE_SCORES = """
if tonumber(redis.call('TIME')[1]) >= tonumber(ARGV[1]) then
    return -1
end
if redis.call('SISMEMBER', KEYS[4], ARGV[2]) == 1 then
    return 0
end
for i = 3, #ARGV, 4 do
    local uid = ARGV[i]
    local score = tonumber(redis.call('ZSCORE', KEYS[1], uid) or '0')
    redis.call('ZADD', KEYS[1], math.max(0, score + tonumber(ARGV[i + 3])), uid)
    redis.call('HSET', KEYS[2], uid, ARGV[i + 1])
    redis.call('HSET', KEYS[3], uid, ARGV[i + 2])
end
redis.call('SADD', KEYS[4], ARGV[2])
for _, key in ipairs(KEYS) do
    redis.call('EXPIREAT', key, ARGV[1])
end
return 1
"""


async def save_scores(
    feature: str,
    chat_id: int,
    players: list[tuple[int, str, str | None, int]],
    redis: RedisStorage,
    day: date | None = None,
    *,
    round_token: str,
) -> None:
    if not players:
        return
    day = day or today()
    key = score_key(feature, chat_id, day)
    args: list[str | int] = [int(score_expiry(day).timestamp()), round_token]
    for user_id, name, username, delta in players:
        args.extend((str(user_id), name, username or "", delta))
    client = await redis.redis()
    result = await cast(Awaitable[int], client.eval(_SAVE_SCORES, 4, key, key + ":names", key + ":usernames", key + ":rounds", *args))
    if result == -1:
        raise ScoreWindowExpired()


async def ranking(feature: str, message: Message, redis: RedisStorage, day: date) -> Text:
    key = score_key(feature, message.chat.id, day)
    client = await redis.redis()
    scores = await cast(Awaitable[list[tuple[str, float]]], client.zrevrange(key, 0, 9, withscores=True))
    rows: list[Text] = []
    for user_id, score in scores:
        name = await cast(Awaitable[str | None], client.hget(key + ":names", user_id))
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        username = await cast(Awaitable[str | None], client.hget(key + ":usernames", user_id))
        if isinstance(username, bytes):
            username = username.decode("utf-8", errors="replace")
        rows.append(Text(f"{len(rows) + 1}. ", user_label(int(user_id), name or "Игрок", username), f" — {int(score)}"))
    body = Text(*[Text(row, "\n") for row in rows]) if rows else Text(f"Пока нет очков. Начни /{feature}")
    heading = "Шахматный рейтинг" if feature == "chess" else "Рейтинг"
    return Text(f"🏆 {heading} за сегодня, {day:%d.%m.%Y} (МСК)\n\n", body)

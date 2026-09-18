"""Daily points and bounded leaderboard views over typed feature records."""

import asyncio
from datetime import date
from heapq import nsmallest
from zoneinfo import ZoneInfo

from aiogram.utils.formatting import Text

from msu_hub_bot.commands.quiz_view import user_label
from msu_hub_bot.games.models import Score, Vote
from msu_hub_bot.storage.features import Collection, JobHold, Record, Scope, Transaction

DAY_ZONE = ZoneInfo("Europe/Moscow")
SCORE_BATCH_SIZE = 30


def score_key(day: date, user_id: int) -> str:
    return f"{day.isoformat()}:{user_id}"


async def apply_batch(collection: Collection[Score], tx: Transaction, day: date, votes: list[Record[Vote]], answer: int) -> None:
    """Stage scores and vote guards; the caller commits its progress alongside them."""
    if len(votes) > SCORE_BATCH_SIZE:
        raise ValueError("Quiz score batch exceeds its transaction budget")
    current = await asyncio.gather(*(collection.get(tx.scope, score_key(day, vote.value.user_id)) for vote in votes))
    for vote, old in zip(votes, current, strict=True):
        key = score_key(day, vote.value.user_id)
        tx.expect(vote)
        if old is None:
            tx.expect_absent(collection.name, key)
            value = Score(user_id=vote.value.user_id, points=0, name=vote.value.name, username=vote.value.username)
        else:
            if old.value.user_id != vote.value.user_id or old.parent != day.isoformat():
                raise JobHold("Quiz score identity is inconsistent")
            tx.expect(old)
            value = old.value.model_copy(deep=True)
            value.name, value.username = vote.value.name, vote.value.username
        value.points = max(0, value.points + (1 if vote.value.choice == answer else -1))
        tx.put(collection, key, value, parent=day.isoformat())


async def ranking(collection: Collection[Score], scope: Scope, day: date) -> Text:
    leaders: list[Score] = []
    after = None
    while True:
        page = await collection.list(scope, parent=day.isoformat(), after=after, limit=200)
        if any(record.key != score_key(day, record.value.user_id) for record in page):
            raise JobHold("Quiz score identity is inconsistent")
        leaders = nsmallest(10, [*leaders, *(record.value for record in page)], key=lambda score: (-score.points, score.user_id))
        if len(page) < 200:
            break
        after = page[-1].key
    rows = [
        Text(f"{index}. ", user_label(score.user_id, score.name, score.username), f" — {score.points}\n")
        for index, score in enumerate(leaders, 1)
    ]
    body = Text(*rows) if rows else Text(f"Пока нет очков. Начни /{collection.feature}")
    heading = "Шахматный рейтинг" if collection.feature == "chess" else "Рейтинг"
    return Text(f"🏆 {heading} за сегодня, {day:%d.%m.%Y} (МСК)\n\n", body)

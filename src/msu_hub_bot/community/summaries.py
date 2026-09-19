"""Bounded, read-only game summaries without exposing active puzzle answers."""

import asyncio
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from msu_hub_bot.games.chess_play.records import Rating, SavedMatch
from msu_hub_bot.games.models import RoundState, Score
from msu_hub_bot.storage.features import Collection, FeatureProtocolError, FeatureStore, Payload, Record, Scope

type GameKind = Literal["chess", "geoguess", "chess_play"]
PAGE_LIMIT = 10


async def bounded[M: Payload](collection: Collection[M], scope: Scope, *, parent: str | None = None) -> tuple[list[Record[M]], bool]:
    rows: list[Record[M]] = []
    after = None
    for _ in range(PAGE_LIMIT):
        page = await collection.list(scope, parent=parent, after=after, limit=200)
        rows.extend(page)
        if len(page) < 200:
            return rows, False
        after = page[-1].key
    return rows, True


class GameSummaries:
    def __init__(self, store: FeatureStore, bot_id: int) -> None:
        self.bot_id = bot_id
        self.rounds = {kind: store.collection(kind, "rounds", RoundState, retention=None) for kind in ("chess", "geoguess")}
        self.scores = {kind: store.collection(kind, "scores", Score, retention=None) for kind in ("chess", "geoguess")}
        self.matches = store.collection("chess_play", "matches", SavedMatch, retention=None)
        self.ratings = store.collection("chess_play", "ratings", Rating, retention=None)
        self._reads = asyncio.Semaphore(2)

    async def read(self, chat_id: int, thread_id: int | None, kind: GameKind, now: datetime) -> dict[str, object]:
        async with self._reads:
            return await (self._matches(chat_id, thread_id) if kind == "chess_play" else self._quiz(chat_id, thread_id, kind, now))

    async def _quiz(self, chat_id: int, thread_id: int | None, kind: Literal["chess", "geoguess"], now: datetime) -> dict[str, object]:
        scope = Scope(f"chat:{chat_id}")
        day = now.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
        scores, truncated_scores = await bounded(self.scores[kind], scope, parent=day)
        rounds, truncated_rounds = await bounded(self.rounds[kind], scope)
        if any(row.key != f"{day}:{row.value.user_id}" for row in scores) or any(
            row.value.chat_id != chat_id or row.key != row.value.token for row in rounds
        ):
            raise FeatureProtocolError()
        leaders = sorted((row.value for row in scores), key=lambda value: (-value.points, value.user_id))[:20]
        history = [row for row in rounds if row.value.thread_id == thread_id and row.value.phase not in {"preparing", "publishing"}]
        history.sort(key=lambda row: row.value.prepared_at, reverse=True)
        note = f"Рейтинг всего чата за {day} (МСК). История этой темы хранится сутки после завершения; очки — постоянно."
        if truncated_rounds or truncated_scores:
            note += " Показана ограниченная выборка; полный список доступен в командах бота."
        return {
            "rankings": [
                {"user_id": value.user_id, "name": value.name, "score": value.points, "played": None, "correct": None} for value in leaders
            ],
            "history": [
                {
                    "key": row.key,
                    "title": "Шахматная задача" if kind == "chess" else "Где это снято?",
                    "status": "cancelled" if row.value.phase == "abandoned" else row.value.phase,
                    "created_at": row.value.prepared_at.isoformat(),
                    "finished_at": row.value.closed_at.isoformat() if row.value.closed_at else None,
                }
                for row in history[:30]
            ],
            "retention_note": note,
        }

    async def _matches(self, chat_id: int, thread_id: int | None) -> dict[str, object]:
        scope = Scope("global")
        matches, truncated = await bounded(self.matches, scope, parent=str(chat_id))
        if any(
            row.value.game.chat_id != chat_id or row.value.game.bot_id != self.bot_id or row.key != f"{chat_id}:{row.value.game.token}"
            for row in matches
        ):
            raise FeatureProtocolError()
        visible = [row for row in matches if row.value.game.thread_id == thread_id and row.value.publication == "bound"]
        visible.sort(key=lambda row: row.value.game.created_at, reverse=True)
        # Elo is global, but this view only includes people visible in this
        # chat's retained matches; it cannot enumerate players in other chats.
        players = {player.user_id for row in visible for player in (row.value.game.white, row.value.game.black) if player is not None}
        ratings: list[Rating] = []
        for user_id in sorted(players)[:60]:
            row = await self.ratings.get(scope, str(user_id))
            if row is not None:
                if row.value.user_id != user_id:
                    raise FeatureProtocolError()
                ratings.append(row.value)
        ratings.sort(key=lambda value: (-value.rating, value.user_id))
        note = "Партии этой темы хранятся сутки после завершения. Постоянный Elo — общий у бота; здесь участники видимых партий."
        if truncated or len(players) > 60:
            note += " Показана ограниченная выборка."
        return {
            "rankings": [
                {"user_id": value.user_id, "name": value.name, "score": value.rating, "played": None, "correct": None}
                for value in ratings[:20]
            ],
            "history": [
                {
                    "key": row.key,
                    "title": row.value.game.white.name + " — " + (row.value.game.black.name if row.value.game.black else "ждём соперника"),
                    "status": "invitation" if row.value.game.status == "waiting" else row.value.game.status,
                    "created_at": row.created_at.isoformat(),
                    "finished_at": row.value.finished_at.isoformat() if row.value.finished_at else None,
                }
                for row in visible[:30]
            ],
            "retention_note": note,
        }

"""Leased chess clocks, conditional moves and atomic bot-wide Elo settlement."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from aiogram.methods import EditMessageCaption, EditMessageMedia, TelegramMethod
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto, Message, User
from cachetools import LRUCache

from msu_hub_bot.commands.chess_play_view import PlayCallback, keyboard, render
from msu_hub_bot.commands.quiz_view import compact
from msu_hub_bot.media.chess_play_board import render_match
from msu_hub_bot.storage.features import (
    Conflict,
    FeatureStore,
    FeatureWorker,
    JobContext,
    JobHold,
    JobRetry,
    Record,
    RecordKey,
    Scope,
    Transaction,
)
from msu_hub_bot.storage.supabase import RepositoryUnavailable

from .models import Game, GameError, Player
from .records import INITIAL_RATING, ChatMatch, RatedPlayer, Rating, RatingPage, SavedMatch, elo_delta

FEATURE = "chess_play"
SCOPE = Scope("global")
PUBLICATION_WINDOW = timedelta(minutes=1)
RESULT_WINDOW = timedelta(days=1)
CLOCK_INTERVAL = timedelta(seconds=5)
MAX_RATING_PAGES = 1000
SEND_TIMEOUT = 15
_Result = TypeVar("_Result")
logger = logging.getLogger(__name__)


@dataclass
class Presentation:
    image_key: tuple[str, str, str, str | None, int | None] | None = None
    last_edit: float = 0.0


class ChessMatchService:
    """One active match per chat; only rendering caches live in process memory."""

    def __init__(self, bot: Bot, store: FeatureStore, worker: FeatureWorker) -> None:
        self.bot, self.store, self.worker = bot, store, worker
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.chats = store.collection(FEATURE, "chats", ChatMatch, retention=timedelta(days=30))
        self.matches = store.collection(FEATURE, "matches", SavedMatch, retention=None)
        self.ratings = store.collection(FEATURE, "ratings", Rating, retention=None)
        self._presentations: LRUCache[str, Presentation] = LRUCache(maxsize=1024)
        self._rendering = asyncio.Semaphore(4)
        self._rankings = asyncio.Semaphore(2)
        for kind, handler in (
            ("deadline", self._deadline),
            ("settle", self._settle),
            ("render", self._render),
            ("cleanup", self._cleanup),
        ):
            worker.register(FEATURE, kind, self._retryable(handler), max_attempts=1024)

    @staticmethod
    def _retryable(handler: Callable[[JobContext], Awaitable[None]]) -> Callable[[JobContext], Awaitable[None]]:
        async def execute(context: JobContext) -> None:
            try:
                await handler(context)
            except Conflict, RepositoryUnavailable, TelegramAPIError, TimeoutError:
                if not await context.current():
                    # Complete our obsolete lease so the newer generation can
                    # render immediately instead of waiting for lease expiry.
                    return
                raise JobRetry("Conditional chess work can safely be replayed") from None

        return execute

    def _tx(self) -> Transaction:
        return self.store.transaction(FEATURE, SCOPE, operation_id=uuid4().hex)

    @staticmethod
    async def _commit(tx: Transaction) -> None:
        try:
            await tx.commit()
        except RepositoryUnavailable, TimeoutError:
            await tx.commit()

    async def _send(self, method: TelegramMethod[_Result]) -> _Result:
        async with asyncio.timeout(SEND_TIMEOUT):
            return await self.bot(method, request_timeout=SEND_TIMEOUT)

    @staticmethod
    def _player(user: User) -> Player:
        return Player(user_id=user.id, name=compact(user.full_name, 256), username=user.username)

    @staticmethod
    def token(bot_id: int, chat_id: int, message_id: int) -> str:
        return hashlib.blake2s(f"{bot_id}:{chat_id}:{message_id}".encode(), digest_size=6).hexdigest()

    @staticmethod
    def key(chat_id: int, token: str) -> str:
        return f"{chat_id}:{token}"

    async def get(self, chat_id: int, token: str) -> Record[SavedMatch] | None:
        if re.fullmatch(r"[0-9a-f]{12}", token) is None:
            raise GameError("Эта партия недоступна.")
        row = await self.matches.get(SCOPE, self.key(chat_id, token))
        if row is not None and (row.value.game.bot_id, row.value.game.chat_id, row.value.game.token) != (self.bot.id, chat_id, token):
            raise GameError("Не удалось проверить партию.")
        return row

    async def _job_match(self, context: JobContext) -> Record[SavedMatch] | None:
        if context.job.scope != SCOPE or context.job.record.collection != "matches":
            raise JobHold("Chess job identity is inconsistent")
        row = await self.matches.get(SCOPE, context.job.record.key)
        if row is not None and (row.value.game.bot_id != self.bot.id or row.key != self.key(row.value.game.chat_id, row.value.game.token)):
            raise JobHold("Chess match identity is inconsistent")
        return row

    def _schedule(self, tx: Transaction, row: Record[SavedMatch], kind: str, when: datetime) -> None:
        tx.schedule(f"{kind}:{row.key}", kind, record=RecordKey("matches", row.key), run_at=when)

    async def _transition(
        self,
        row: Record[SavedMatch],
        value: SavedMatch,
        *,
        tx: Transaction | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        tx = tx or self._tx()
        tx.expect(row)
        game = value.game
        if game.status == "finished":
            value = value.model_copy(
                update={
                    "finished_at": value.finished_at or finished_at or self.clock(),
                    "rating_status": "skipped" if game.black is None else value.rating_status,
                }
            )
            assert value.finished_at is not None
            chat = await self.chats.get(SCOPE, str(game.chat_id))
            if chat is not None and chat.value.active == row.key:
                changed = chat.value.model_copy(deep=True)
                changed.active = None
                tx.expect(chat)
                tx.put(self.chats, chat.key, changed)
            tx.cancel_job(f"deadline:{row.key}")
            if value.rating_status == "pending":
                self._schedule(tx, row, "settle", self.clock())
            self._schedule(tx, row, "cleanup", max(self.clock(), value.finished_at + RESULT_WINDOW))
        else:
            due = value.publication_due if value.publication == "publishing" else datetime.fromtimestamp(game.deadline() or 0, UTC)
            self._schedule(tx, row, "deadline", due)
        if value.publication == "bound":
            self._schedule(tx, row, "render", self.clock())
        tx.put(self.matches, row.key, value, parent=str(game.chat_id), status=game.status)
        await self._commit(tx)

    async def _abandon(self, row: Record[SavedMatch]) -> None:
        if row.value.publication != "publishing":
            return
        value = row.value.model_copy(deep=True)
        value.game.cancel(value.game.white.user_id, min(self.clock().timestamp(), value.game.invite_deadline - 0.001))
        value = value.model_copy(update={"publication": "abandoned"})
        await self._transition(row, value)

    async def _expire(self, row: Record[SavedMatch]) -> bool:
        if row.value.publication == "publishing":
            if self.clock() >= row.value.publication_due:
                await self._abandon(row)
                return True
            return False
        value = row.value.model_copy(deep=True)
        deadline = value.game.deadline()
        if value.game.expire(self.clock().timestamp()):
            await self._transition(row, value, finished_at=datetime.fromtimestamp(deadline, UTC) if deadline is not None else None)
            return True
        return False

    async def _bind(self, row: Record[SavedMatch], message: Message) -> None:
        for _ in range(4):
            current = await self.get(row.value.game.chat_id, row.value.game.token)
            if current is None:
                raise GameError("Приглашение уже недоступно.")
            if current.value.publication == "bound" and current.value.game.message_id == message.message_id:
                return
            if current.value.publication != "publishing" or self.clock() >= current.value.publication_due:
                raise GameError("Приглашение уже недоступно.")
            value = current.value.model_copy(deep=True)
            value.game.message_id = message.message_id
            value.publication = "bound"
            try:
                await self._transition(current, value)
                return
            except Conflict:
                continue
        raise Conflict()

    async def start(self, message: Message) -> Message | None:
        user = message.from_user
        if message.chat.type not in {"group", "supergroup"} or user is None or user.is_bot or message.sender_chat is not None:
            return await self._send(message.reply("Начни партию от своего имени в групповом чате."))
        token = self.token(self.bot.id, message.chat.id, message.message_id)
        match_key = self.key(message.chat.id, token)
        for _ in range(4):
            chat = await self.chats.get(SCOPE, str(message.chat.id))
            existing = await self.get(message.chat.id, token)
            if existing is not None:
                return await self._send(message.reply("Приглашение уже создано. Используй кнопки под доской."))
            if chat is not None and chat.value.active is not None:
                if not re.fullmatch(rf"{message.chat.id}:[0-9a-f]{{12}}", chat.value.active):
                    raise GameError("Не удалось проверить приглашение в этом чате.")
                active = await self.matches.get(SCOPE, chat.value.active)
                if active is None:
                    cleared = chat.value.model_copy(deep=True)
                    cleared.active = None
                    tx = self._tx()
                    tx.expect(chat)
                    tx.expect_absent("matches", chat.value.active)
                    tx.put(self.chats, chat.key, cleared)
                    try:
                        await self._commit(tx)
                    except Conflict:
                        pass
                    continue
                if (active.value.game.bot_id, active.value.game.chat_id, self.key(message.chat.id, active.value.game.token)) != (
                    self.bot.id,
                    message.chat.id,
                    chat.value.active,
                ):
                    raise GameError("Не удалось проверить приглашение в этом чате.")
                if active is not None and await self._expire(active):
                    continue
                return await self._send(message.reply("Подождите, текущая партия ещё не завершена!"))
            now = self.clock()
            rating = await self.ratings.get(SCOPE, str(user.id))
            game = Game(
                token=token,
                bot_id=self.bot.id,
                chat_id=message.chat.id,
                thread_id=message.message_thread_id,
                white=self._player(user),
                white_rating=INITIAL_RATING if rating is None else rating.value.rating,
                created_at=now.timestamp(),
                invite_deadline=(now + timedelta(minutes=10)).timestamp(),
            )
            saved = SavedMatch(game=game, source_message_id=message.message_id, publication_due=now + PUBLICATION_WINDOW)
            pointer = ChatMatch() if chat is None else chat.value.model_copy(deep=True)
            pointer.active = match_key
            tx = self._tx()
            if chat is None:
                tx.expect_absent("chats", str(message.chat.id))
            else:
                tx.expect(chat)
            tx.expect_absent("matches", match_key)
            tx.put(self.chats, str(message.chat.id), pointer, expires_at=None)
            tx.put(self.matches, match_key, saved, parent=str(message.chat.id), status="waiting")
            tx.schedule(f"deadline:{match_key}", "deadline", record=RecordKey("matches", match_key), run_at=saved.publication_due)
            try:
                await self._commit(tx)
            except Conflict:
                continue
            break
        else:
            return await self._send(message.reply("Приглашение изменилось. Попробуй ещё раз."))
        row = await self.get(message.chat.id, token)
        if row is None:
            raise GameError("Не удалось сохранить приглашение.")
        sending = False
        try:
            async with asyncio.timeout(10), self._rendering:
                photo = await asyncio.to_thread(render_match, row.value.game)
            view = render(row.value.game, self.clock().timestamp())
            sending = True
            sent = await self._send(
                message.reply_photo(
                    BufferedInputFile(photo, filename="chess.png"),
                    caption=view.caption,
                    caption_entities=view.entities,
                    parse_mode=None,
                    reply_markup=keyboard(row.value.game),
                )
            )
            await self._bind(row, sent)
            self._presentations[row.key] = Presentation(self._image_key(row.value.game), asyncio.get_running_loop().time())
        except TelegramBadRequest, TelegramForbiddenError:
            await self._abandon(row)
            return await self._send(message.reply("Не удалось показать доску. Попробуй ещё раз."))
        except asyncio.CancelledError:
            if not sending:
                await asyncio.shield(self._abandon(row))
            raise
        except Exception:
            logger.exception("Chess match publication could not be confirmed")
            if not sending:
                await self._abandon(row)
                return await self._send(message.reply("Не удалось нарисовать доску. Попробуй ещё раз."))
            return await self._send(
                message.reply("Не удалось подтвердить отправку. Если доска появилась, нажми её кнопку; иначе попробуй через минуту.")
            )
        return None

    def _message_matches(self, row: Record[SavedMatch], message: Message) -> bool:
        game = row.value.game
        if (
            message.chat.id != game.chat_id
            or message.message_thread_id != game.thread_id
            or message.from_user is None
            or message.from_user.id != self.bot.id
            or not message.from_user.is_bot
            or message.forward_origin is not None
        ):
            return False
        if row.value.publication == "bound":
            return game.message_id == message.message_id
        expected = keyboard(game)
        return (
            row.value.publication == "publishing"
            and self.clock() < row.value.publication_due
            and bool(message.photo)
            and message.reply_to_message is not None
            and message.reply_to_message.message_id == row.value.source_message_id
            and message.reply_markup is not None
            and expected is not None
            and message.reply_markup.model_dump(mode="json") == expected.model_dump(mode="json")
        )

    async def _pair(self, tx: Transaction, game: Game) -> None:
        black = game.black
        assert black is not None
        players = (game.white, black)
        rows = await asyncio.gather(*(self.ratings.get(SCOPE, str(player.user_id)) for player in players))
        values = []
        for player, row in zip(players, rows, strict=True):
            value = (
                Rating(user_id=player.user_id, name=player.name, username=player.username)
                if row is None
                else row.value.model_copy(deep=True)
            )
            if value.user_id != player.user_id:
                raise GameError("Не удалось проверить рейтинг игрока.")
            value.name, value.username = player.name, player.username
            if row is None:
                tx.expect_absent("ratings", str(player.user_id))
            else:
                tx.expect(row)
            tx.put(self.ratings, str(player.user_id), value)
            values.append(value.rating)
        game.white_rating, game.black_rating = values

    async def _apply(self, tx: Transaction, game: Game, data: PlayCallback, user: User) -> None:
        now = self.clock().timestamp()
        if data.action == "join":
            game.join(self._player(user), now)
            await self._pair(tx, game)
        elif data.action == "pick":
            game.select(user.id, data.value, now)
        elif data.action == "back":
            game.select(user.id, None, now)
        elif data.action == "move":
            game.move(user.id, data.value, now)
        elif data.action == "cancel":
            game.cancel(user.id, now)
        elif data.action == "resign":
            game.resign(user.id, now)
        elif data.action == "draw":
            game.offer_draw(user.id, now)
        elif data.action == "accept_draw":
            game.accept_draw(user.id, now)
        elif data.action == "decline_draw":
            game.decline_draw(user.id, now)
        elif data.action == "claim_draw":
            game.claim_draw(user.id, now)
        else:
            raise GameError("Эта кнопка больше не действует.")

    async def callback(self, query: CallbackQuery, callback_data: PlayCallback) -> None:
        answer = ""
        try:
            if not isinstance(query.message, Message) or query.from_user.is_bot:
                raise GameError("Доска недоступна.")
            for _ in range(4):
                row = await self.get(query.message.chat.id, callback_data.game)
                if row is None or not self._message_matches(row, query.message):
                    raise GameError("Эта партия уже недоступна.")
                if row.value.publication == "publishing":
                    try:
                        await self._bind(row, query.message)
                    except Conflict:
                        pass
                    continue
                try:
                    if await self._expire(row) or row.value.game.status == "finished":
                        raise GameError("Партия уже завершена.")
                    game = row.value.game
                    if game.black is not None and query.from_user.id not in {game.white.user_id, game.black.user_id}:
                        raise GameError("Вы наблюдаете за партией.")
                    if callback_data.revision != game.revision:
                        raise GameError("Доска обновилась. Нажми кнопку ещё раз.")
                    value = row.value.model_copy(deep=True)
                    tx = self._tx()
                    try:
                        await self._apply(tx, value.game, callback_data, query.from_user)
                    except GameError:
                        if value.game.status == "finished":
                            deadline = row.value.game.deadline()
                            await self._transition(
                                row, value, finished_at=datetime.fromtimestamp(deadline, UTC) if deadline is not None else None
                            )
                        raise
                    await self._transition(row, value, tx=tx)
                except Conflict:
                    continue
                break
            else:
                raise GameError("Доска обновилась. Нажми кнопку ещё раз.")
        except GameError as error:
            answer = str(error)
        except Exception:
            logger.exception("Chess action could not be confirmed")
            answer = "Не удалось подтвердить действие. Проверь доску и попробуй ещё раз."
        try:
            await self._send(query.answer(answer))
        except TelegramAPIError, TimeoutError:
            pass

    async def _deadline(self, context: JobContext) -> None:
        row = await self._job_match(context)
        if row is None or not await context.current():
            return
        if not await self._expire(row) and row.value.game.status != "finished":
            due = (
                row.value.publication_due
                if row.value.publication == "publishing"
                else datetime.fromtimestamp(row.value.game.deadline() or 0, UTC)
            )
            tx = self._tx()
            tx.expect(row)
            self._schedule(tx, row, "deadline", due)
            await self._commit(tx)

    async def _settle(self, context: JobContext) -> None:
        row = await self._job_match(context)
        if row is None or row.value.rating_status != "pending" or row.value.game.status != "finished":
            return
        game = row.value.game
        black = game.black
        if black is None or game.winner not in {None, game.white.user_id, black.user_id}:
            raise JobHold("Chess settlement has incomplete participants")
        players = (game.white, black)
        ratings = await asyncio.gather(*(self.ratings.get(SCOPE, str(player.user_id)) for player in players))
        if any(rating is None for rating in ratings):
            raise JobHold("Chess settlement is missing a starting player")
        score = 0.5 if game.winner is None else float(game.winner == game.white.user_id)
        delta = elo_delta(game.white_rating, game.black_rating, score)
        tx = self._tx()
        tx.expect(row)
        changes = []
        for player, rating, change in zip(players, ratings, (delta, -delta), strict=True):
            assert rating is not None
            if rating.value.user_id != player.user_id:
                raise JobHold("Chess rating identity is inconsistent")
            value = rating.value.model_copy(deep=True)
            before = value.rating
            value.rating += change
            value.name, value.username = player.name, player.username
            tx.expect(rating)
            tx.put(self.ratings, rating.key, value)
            changes.append((before, value.rating))
        saved = row.value.model_copy(deep=True)
        saved = saved.model_copy(update={"ratings": (changes[0], changes[1]), "rating_status": "settled"})
        tx.put(self.matches, row.key, saved, parent=str(game.chat_id), status="finished")
        self._schedule(tx, row, "render", self.clock())
        if saved.finished_at is None:
            raise JobHold("Chess settlement is missing its closing time")
        self._schedule(tx, row, "cleanup", max(self.clock(), saved.finished_at + RESULT_WINDOW))
        if not await context.current():
            return
        await self._commit(tx)

    @staticmethod
    def _image_key(game: Game) -> tuple[str, str, str, str | None, int | None]:
        return game.initial_fen, " ".join(game.moves), game.status, game.result, game.winner

    async def _render(self, context: JobContext) -> None:
        row = await self._job_match(context)
        if row is None or row.value.publication != "bound" or row.value.game.message_id is None:
            return
        game = row.value.game
        presentation = self._presentations.setdefault(row.key, Presentation())
        await asyncio.sleep(max(0, presentation.last_edit + 1 - asyncio.get_running_loop().time()))
        view = render(game, self.clock().timestamp(), ratings=row.value.ratings)
        image_key = self._image_key(game)
        failed = False
        method: TelegramMethod[Message | bool]
        try:
            if presentation.image_key != image_key:
                async with asyncio.timeout(10), self._rendering:
                    photo = await asyncio.to_thread(render_match, game)
                method = EditMessageMedia(
                    chat_id=game.chat_id,
                    message_id=game.message_id,
                    media=InputMediaPhoto(
                        media=BufferedInputFile(photo, filename="chess.png"),
                        caption=view.caption,
                        caption_entities=view.entities,
                        parse_mode=None,
                    ),
                    reply_markup=keyboard(game),
                )
            else:
                method = EditMessageCaption(
                    chat_id=game.chat_id,
                    message_id=game.message_id,
                    caption=view.caption,
                    caption_entities=view.entities,
                    parse_mode=None,
                    reply_markup=keyboard(game),
                )
            if not await context.current():
                return
            await self._send(method)
            presentation.image_key = image_key
        except TelegramBadRequest as error:
            if "message is not modified" in error.message.casefold():
                presentation.image_key = image_key
            elif any(reason in error.message.casefold() for reason in ("message to edit not found", "message can't be edited")):
                failed = True
            else:
                raise JobHold("Chess board presentation was rejected") from None
        except TelegramForbiddenError, TelegramNotFound:
            failed = True
        presentation.last_edit = asyncio.get_running_loop().time()
        tx = self._tx()
        tx.expect(row)
        if game.status == "finished" or failed:
            value = row.value.model_copy(deep=True)
            value.presentation_failed = failed
            value.revealed = game.status == "finished" and value.rating_status != "pending" and not failed
            tx.put(self.matches, row.key, value, parent=str(game.chat_id), status=game.status)
        elif game.status == "playing":
            self._schedule(tx, row, "render", self.clock() + CLOCK_INTERVAL)
        if not await context.current():
            return
        await self._commit(tx)

    async def _cleanup(self, context: JobContext) -> None:
        row = await self._job_match(context)
        if row is None:
            return
        value = row.value
        if value.game.status != "finished" or value.rating_status == "pending" or value.finished_at is None:
            raise JobHold("Chess cleanup awaits completed settlement")
        if self.clock() < value.finished_at + RESULT_WINDOW:
            raise JobRetry("Chess result window is still open")
        tx = self._tx()
        tx.expect(row)
        for kind in ("deadline", "render", "settle", "cleanup"):
            tx.cancel_job(f"{kind}:{row.key}")
        tx.delete(row)
        if not await context.current():
            return
        await self._commit(tx)
        self._presentations.pop(row.key, None)

    async def _rating_rows(self) -> AsyncIterator[Rating]:
        after = None
        while True:
            batch = await self.ratings.list(SCOPE, after=after, limit=200)
            for row in batch:
                if row.key != str(row.value.user_id):
                    raise GameError("Не удалось проверить рейтинг.")
                yield row.value
            if len(batch) < 200:
                return
            after = batch[-1].key

    @staticmethod
    def _rated(value: Rating) -> RatedPlayer:
        return RatedPlayer(user_id=value.user_id, name=value.name, username=value.username, rating=value.rating)

    async def rating(self, user_id: int, *, page: int = 0) -> tuple[RatingPage, RatedPlayer]:
        if user_id <= 0:
            raise GameError("Рейтинг доступен от личного аккаунта.")
        # Key-ordered pages need a complete scan. Count/rank first, then keep only
        # the requested end of the ranking; never export partial scan results.
        async with asyncio.timeout(5), self._rankings:
            own = await self.ratings.get(SCOPE, str(user_id))
            if own is not None and own.value.user_id != user_id:
                raise GameError("Не удалось проверить рейтинг.")
            person = RatedPlayer(user_id=user_id, name="Игрок") if own is None else self._rated(own.value)
            total, rank = 0, 1
            async for value in self._rating_rows():
                total += 1
                if (-value.rating, value.user_id) < (-person.rating, person.user_id):
                    rank += 1
            person.rank = rank if own is not None else None
            pages = min(MAX_RATING_PAGES, max(1, (total + 9) // 10))
            page = max(0, min(page, pages - 1))
            start, end = page * 10, min(total, (page + 1) * 10)
            bottom = total - start < end
            capacity = total - start if bottom else end
            best: list[tuple[int, int, RatedPlayer]] = []
            async for value in self._rating_rows():
                priority = (-value.rating, value.user_id) if bottom else (value.rating, -value.user_id)
                item = (*priority, self._rated(value))
                if len(best) < capacity:
                    heapq.heappush(best, item)
                elif best and priority > best[0][:2]:
                    heapq.heapreplace(best, item)
            players = sorted((item[2] for item in best), key=lambda player: (-player.rating, player.user_id))
            selected = players[:10] if bottom else players[start:end]
            for offset, player in enumerate(selected, start + 1):
                player.rank = offset
            return RatingPage(players=tuple(selected), total=total, page=page, pages=pages), person

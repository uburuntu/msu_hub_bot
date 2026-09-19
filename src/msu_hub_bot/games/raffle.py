"""Atomic raffle membership and immutable draws over shared feature documents."""

import asyncio
import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Self
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from msu_hub_bot.storage.features import CommitResult, Conflict, FeatureProtocolError, FeatureStore, Payload, Record, Scope, Transaction
from msu_hub_bot.storage.supabase import RepositoryUnavailable

RETENTION = timedelta(days=90)
PAGE_SIZE = 12
MAX_PARTICIPANTS = 2**31 - 1


class RaffleError(ValueError):
    """Safe explanations for unavailable or disallowed raffle actions."""


class Person(Payload):
    user_id: int = Field(strict=True, gt=0)
    name: str = Field(min_length=1, max_length=256)
    username: str | None = Field(default=None, max_length=32)


class Entry(Payload):
    slot: int = Field(strict=True, ge=0, lt=MAX_PARTICIPANTS)
    person: Person


class Membership(Payload):
    slot: int = Field(strict=True, ge=0, lt=MAX_PARTICIPANTS)


class RaffleRound(Payload):
    token: str = Field(pattern=r"^[a-f0-9]{16}$")
    bot_id: int = Field(strict=True, gt=0)
    chat_id: int = Field(strict=True)
    thread_id: int | None = Field(default=None, strict=True, gt=0)
    source_message_id: int = Field(strict=True, gt=0)
    reply_message_id: int = Field(strict=True, gt=0)
    creator: Person
    created_at: AwareDatetime
    expires_at: AwareDatetime
    message_id: int | None = Field(default=None, strict=True, gt=0)
    participants: int = Field(default=0, strict=True, ge=0, le=MAX_PARTICIPANTS)
    status: Literal["open", "finished"] = "open"
    winner: Person | None = None
    winner_slot: int | None = Field(default=None, strict=True, ge=0)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.chat_id == 0 or self.expires_at <= self.created_at:
            raise ValueError("Invalid raffle identity or retention")
        if self.status == "open" and (self.winner is not None or self.winner_slot is not None):
            raise ValueError("An open raffle cannot have a winner")
        if self.status == "finished" and (
            self.winner is None or self.winner_slot is None or self.winner_slot >= self.participants or self.message_id is None
        ):
            raise ValueError("A finished raffle needs its bound message and winner")
        return self


class RaffleStore:
    def __init__(self, bot_id: int, store: FeatureStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self.bot_id, self.store = bot_id, store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.rounds = store.collection("raffle", "rounds", RaffleRound, retention=RETENTION)
        self.entries = store.collection("raffle", "participants", Entry, retention=RETENTION)
        self.members = store.collection("raffle", "members", Membership, retention=RETENTION)
        self._mutations = [asyncio.Lock() for _ in range(64)]
        self._renders = [asyncio.Lock() for _ in range(64)]

    @staticmethod
    def scope(chat_id: int, thread_id: int | None) -> Scope:
        return Scope(f"chat:{chat_id}:topic:{thread_id or 0}")

    def token(self, chat_id: int, message_id: int) -> str:
        return hashlib.blake2s(f"{self.bot_id}:{chat_id}:{message_id}".encode(), digest_size=8).hexdigest()

    @staticmethod
    def entry_key(token: str, slot: int) -> str:
        return f"{token}:{slot:012d}"

    def render_lock(self, token: str) -> asyncio.Lock:
        return self._renders[hash(token) % len(self._renders)]

    def _mutation_lock(self, token: str) -> asyncio.Lock:
        return self._mutations[hash(token) % len(self._mutations)]

    def _tx(self, scope: Scope) -> Transaction:
        return self.store.transaction("raffle", scope, operation_id=uuid4().hex)

    @staticmethod
    async def _commit(tx: Transaction) -> CommitResult:
        try:
            return await tx.commit()
        except RepositoryUnavailable, TimeoutError:
            return await tx.commit()

    def _changed(self, result: CommitResult, scope: Scope, token: str) -> Record[RaffleRound]:
        raw = next((row for row in result.records if row.collection == "rounds" and row.key == token), None)
        if raw is None:
            raise FeatureProtocolError()
        return self.rounds.decode(raw, scope)

    async def get(self, scope: Scope, token: str) -> Record[RaffleRound] | None:
        if not re.fullmatch(r"[a-f0-9]{16}", token):
            raise RaffleError("Эта кнопка уже недоступна. Начни новый /raffle.")
        row = await self.rounds.get(scope, token)
        if row is not None:
            value = row.value
            if (value.token, value.bot_id, self.scope(value.chat_id, value.thread_id)) != (token, self.bot_id, scope):
                raise FeatureProtocolError()
            if self.clock() >= value.expires_at:
                return None
        return row

    async def require(self, scope: Scope, token: str) -> Record[RaffleRound]:
        row = await self.get(scope, token)
        if row is None:
            raise RaffleError("Этот розыгрыш уже закрыт или недоступен. Начни новый /raffle.")
        return row

    async def create(
        self, chat_id: int, thread_id: int | None, source_message_id: int, reply_message_id: int, creator: Person
    ) -> tuple[Record[RaffleRound], bool]:
        token, scope = self.token(chat_id, source_message_id), self.scope(chat_id, thread_id)
        async with self._mutation_lock(token):
            current = await self.get(scope, token)
            if current is not None:
                return current, False
            now = self.clock()
            value = RaffleRound(
                token=token,
                bot_id=self.bot_id,
                chat_id=chat_id,
                thread_id=thread_id,
                source_message_id=source_message_id,
                reply_message_id=reply_message_id,
                creator=creator,
                created_at=now,
                expires_at=now + RETENTION,
            )
            tx = self._tx(scope)
            tx.expect_absent("rounds", token)
            tx.put(self.rounds, token, value, expires_at=value.expires_at)
            try:
                return self._changed(await self._commit(tx), scope, token), True
            except Conflict:
                return await self.require(scope, token), False

    async def bind(self, scope: Scope, token: str, message_id: int) -> Record[RaffleRound]:
        async with self._mutation_lock(token):
            for _ in range(12):
                row = await self.require(scope, token)
                if row.value.message_id is not None:
                    if row.value.message_id != message_id:
                        raise RaffleError("Эта кнопка относится к другому сообщению.")
                    return row
                tx = self._tx(scope)
                tx.expect(row)
                tx.put(self.rounds, token, row.value.model_copy(update={"message_id": message_id}), expires_at=row.value.expires_at)
                try:
                    return self._changed(await self._commit(tx), scope, token)
                except Conflict:
                    continue
        raise Conflict()

    async def join(self, scope: Scope, token: str, person: Person) -> tuple[Record[RaffleRound], bool]:
        async with self._mutation_lock(token):
            for _ in range(12):
                row = await self.require(scope, token)
                value = row.value
                if value.message_id is None:
                    raise RaffleError("Розыгрыш ещё открывается. Нажми кнопку чуть позже.")
                if value.status != "open":
                    raise RaffleError("Победитель уже выбран. До следующего розыгрыша! 🎈")
                member_key = f"{token}:{person.user_id}"
                if await self.members.get(scope, member_key) is not None:
                    return row, False
                if value.participants == MAX_PARTICIPANTS:
                    raise RaffleError("Шарики закончились — пора выбирать победителя! 🎈")
                slot = value.participants
                tx = self._tx(scope)
                tx.expect(row)
                tx.expect_absent("members", member_key)
                tx.expect_absent("participants", self.entry_key(token, slot))
                tx.put(self.rounds, token, value.model_copy(update={"participants": slot + 1}), expires_at=value.expires_at)
                tx.put(self.members, member_key, Membership(slot=slot), parent=token, expires_at=value.expires_at)
                tx.put(
                    self.entries, self.entry_key(token, slot), Entry(slot=slot, person=person), parent=token, expires_at=value.expires_at
                )
                try:
                    return self._changed(await self._commit(tx), scope, token), True
                except Conflict:
                    continue
        raise Conflict()

    async def draw(self, scope: Scope, token: str, user_id: int) -> Record[RaffleRound]:
        async with self._mutation_lock(token):
            for _ in range(12):
                row = await self.require(scope, token)
                value = row.value
                if value.creator.user_id != user_id:
                    raise RaffleError("🤷🏻‍♂️ Только создатель розыгрыша может выбрать победителя.")
                if value.status == "finished":
                    return row
                if value.message_id is None or not value.participants:
                    raise RaffleError("Сначала нужны участники. Кто за шариками? 🎈")
                slot = secrets.randbelow(value.participants)
                winner = await self.entries.get(scope, self.entry_key(token, slot))
                if winner is None or winner.value.slot != slot or winner.parent != token:
                    raise FeatureProtocolError()
                tx = self._tx(scope)
                tx.expect(row)
                tx.expect(winner)
                tx.put(
                    self.rounds,
                    token,
                    value.model_copy(update={"status": "finished", "winner": winner.value.person, "winner_slot": slot}),
                    expires_at=value.expires_at,
                )
                try:
                    return self._changed(await self._commit(tx), scope, token)
                except Conflict:
                    continue
        raise Conflict()

    async def page(self, row: Record[RaffleRound], page: int) -> tuple[list[Person], int, int]:
        pages = max(1, (row.value.participants + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(0, page), pages - 1)
        start = page * PAGE_SIZE
        if not row.value.participants:
            return [], page, pages
        entries = await self.entries.list(
            row.scope,
            parent=row.key,
            after=self.entry_key(row.key, start - 1) if start else None,
            limit=min(PAGE_SIZE, max(1, row.value.participants - start)),
        )
        expected = min(PAGE_SIZE, row.value.participants - start)
        if len(entries) != expected or any(
            (entry.key, entry.value.slot) != (self.entry_key(row.key, start + index), start + index) for index, entry in enumerate(entries)
        ):
            raise FeatureProtocolError()
        return [entry.value.person for entry in entries], page, pages

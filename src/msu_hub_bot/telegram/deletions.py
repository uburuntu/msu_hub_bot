"""Idempotent Telegram deletions scheduled through the shared durable worker."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import Message
from pydantic import AwareDatetime, Field

from msu_hub_bot.storage.errors import RepositoryUnavailable
from msu_hub_bot.storage.features import (
    Conflict,
    FeatureStore,
    FeatureWorker,
    InvalidPayload,
    JobContext,
    JobHold,
    JobRetry,
    Payload,
    RecordKey,
    Scope,
)
from msu_hub_bot.telegram.fsm_storage import FEATURE, MAX_CONFLICT_RETRIES, commit_state_change


DELETE_REQUEST_TIMEOUT = 15


class Deletion(Payload):
    bot_id: int = Field(strict=True)
    chat_id: int = Field(strict=True)
    message_id: int = Field(strict=True, gt=0)
    run_at: AwareDatetime
    complete: bool = Field(default=False, strict=True)

    @property
    def key(self) -> str:
        return f"{self.chat_id}:{self.message_id}"

    @property
    def scope(self) -> Scope:
        return Scope(f"bot:{self.bot_id}")


class MessageDeletions:
    def __init__(self, store: FeatureStore, bot: Bot, worker: FeatureWorker) -> None:
        self.store, self.bot = store, bot
        self.records = store.collection(FEATURE, "deletions", Deletion, retention=None)
        worker.register(FEATURE, "delete_message", self._execute, max_attempts=10000)

    async def mark_message_to_delete(self, message: Message, after: int) -> bool:
        return await self.mark_message_to_delete_raw(message.chat.id, message.message_id, after)

    async def mark_message_to_delete_raw(self, chat_id: int, message_id: int, after: int) -> bool:
        if type(after) is not int or not 0 <= after <= 10 * 24 * 60 * 60:
            raise ValueError("Deletion delay is outside the supported range")
        value = Deletion(bot_id=self.bot.id, chat_id=chat_id, message_id=message_id, run_at=datetime.now(UTC) + timedelta(seconds=after))
        for _ in range(MAX_CONFLICT_RETRIES):
            record = await self.records.get(value.scope, value.key)
            transaction = self.store.transaction(FEATURE, value.scope, operation_id=str(uuid4()))
            if record is None:
                transaction.expect_absent(self.records.name, value.key)
            else:
                if (record.value.bot_id, record.value.chat_id, record.value.message_id) != (value.bot_id, value.chat_id, value.message_id):
                    raise InvalidPayload()
                transaction.expect(record)
                value = record.value.model_copy(update={"run_at": value.run_at, "complete": False}, deep=True)
            transaction.put(self.records, value.key, value)
            transaction.schedule(value.key, "delete_message", record=RecordKey(self.records.name, value.key), run_at=value.run_at)
            try:
                await commit_state_change(transaction)
                return True
            except Conflict:
                continue
        raise Conflict()

    async def _execute(self, context: JobContext) -> None:
        try:
            await self._delete(context)
        except RepositoryUnavailable:
            raise JobRetry() from None

    async def _delete(self, context: JobContext) -> None:
        if context.job.record.collection != self.records.name or context.job.record.key != context.job.key:
            raise JobHold()
        record = await self.records.get(context.job.scope, context.job.record.key)
        if record is None or record.value.complete or not await context.current():
            return
        value = record.value
        if value.bot_id != self.bot.id or value.scope != context.job.scope or value.key != context.job.key:
            raise JobHold()
        if value.run_at > datetime.now(UTC):
            raise JobRetry()
        try:
            await self.bot.delete_message(value.chat_id, value.message_id, request_timeout=DELETE_REQUEST_TIMEOUT)
        except TelegramRetryAfter as error:
            await context.status("retry", run_at=datetime.now(UTC) + timedelta(seconds=max(1, error.retry_after)))
            return
        except TelegramForbiddenError:
            pass
        except TelegramBadRequest as error:
            if not any(
                reason in error.message.lower() for reason in ("message to delete not found", "message can't be deleted", "chat not found")
            ):
                raise JobHold() from None
        except TelegramNetworkError, TelegramServerError, TimeoutError:
            # Repeating a deletion cannot send a duplicate message or delete a new ID.
            raise JobRetry() from None
        updated = value.model_copy(update={"complete": True}, deep=True)
        transaction = self.store.transaction(FEATURE, value.scope, operation_id=str(uuid4()))
        transaction.expect(record)
        transaction.put(self.records, value.key, updated, expires_at=datetime.now(UTC) + timedelta(days=7))
        try:
            await commit_state_change(transaction)
        except Conflict:
            # A newer schedule owns the record and its job generation.
            return

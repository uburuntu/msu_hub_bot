"""Author-owned reminders with explicit, recoverable Telegram delivery semantics."""

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.methods import SendMessage
from aiogram.types import ChatMemberRestricted, LinkPreviewOptions
from aiogram.utils.chat_member import ADMINS, MEMBERS
from aiogram.utils.formatting import Text, TextLink

from msu_hub_bot.commands.quiz_view import compact
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

from .models import Failure, Reminder, ReminderError, ReminderStatus, Schedule, upgrade_reminder
from .recurrence import next_occurrence

SEND_TIMEOUT = 15
TERMINAL_RETENTION = timedelta(days=30)
MAX_DELIVERY_ATTEMPTS = 8


def destination(chat_id: int, thread_id: int | None) -> str:
    return f"chat:{chat_id}:topic:{thread_id or 0}"


def when(value: Reminder) -> str:
    return f"{value.due_at.astimezone(ZoneInfo(value.timezone)):%d.%m.%Y %H:%M} ({value.timezone})"


class ReminderService:
    def __init__(self, bot: Bot, store: FeatureStore, worker: FeatureWorker) -> None:
        from msu_hub_bot.community.preferences import Preferences

        self.bot, self.store, self.worker = bot, store, worker
        self.preferences = Preferences(store)
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.items = store.collection("reminders", "items", Reminder, retention=None, version=2, upgrades={1: upgrade_reminder})
        worker.register("reminders", "deliver", self._deliver, max_attempts=1024)
        worker.register("reminders", "reconcile", self._reconcile, max_attempts=1024)
        worker.register("reminders", "cleanup", self._cleanup, max_attempts=1024)

    @staticmethod
    def scope(author_id: int) -> Scope:
        if author_id <= 0:
            raise ReminderError("Напоминания доступны от личного аккаунта.")
        return Scope(f"user:{author_id}")

    def _tx(self, author_id: int) -> Transaction:
        return self.store.transaction("reminders", self.scope(author_id), operation_id=uuid4().hex)

    @staticmethod
    async def _commit(tx: Transaction) -> None:
        try:
            await tx.commit()
        except RepositoryUnavailable, TimeoutError:
            await tx.commit()

    def _schedule(self, tx: Transaction, key: str, kind: str, run_at: datetime) -> None:
        tx.schedule(f"{kind}:{key}", kind, record=RecordKey("items", key), run_at=run_at)

    def _put(self, tx: Transaction, record: Record[Reminder], value: Reminder) -> None:
        tx.expect(record)
        tx.put(self.items, record.key, value, parent=destination(value.chat_id, value.thread_id), status=value.status)

    def _future(self, schedule: Schedule) -> None:
        if schedule.due_at <= self.clock():
            raise ReminderError("Выбери время в будущем.")

    def creation_key(self, *, author_id: int, chat_id: int, thread_id: int | None, source_message_id: int) -> str:
        """Stable identity lets adapters reconcile a repeated request before parsing its date."""
        return hashlib.blake2s(f"{self.bot.id}:{author_id}:{chat_id}:{thread_id}:{source_message_id}".encode(), digest_size=6).hexdigest()

    async def get_creation(self, *, author_id: int, chat_id: int, thread_id: int | None, source_message_id: int) -> Record[Reminder] | None:
        key = self.creation_key(author_id=author_id, chat_id=chat_id, thread_id=thread_id, source_message_id=source_message_id)
        row = await self.items.get(self.scope(author_id), key)
        if row is not None and (row.value.author_id, row.value.chat_id, row.value.thread_id) != (author_id, chat_id, thread_id):
            raise ReminderError("Не удалось проверить владельца напоминания.")
        return row

    async def create(
        self, *, author_id: int, author_name: str, chat_id: int, thread_id: int | None, source_message_id: int, schedule: Schedule
    ) -> Record[Reminder]:
        record, _ = await self.create_with_status(
            author_id=author_id,
            author_name=author_name,
            chat_id=chat_id,
            thread_id=thread_id,
            source_message_id=source_message_id,
            schedule=schedule,
        )
        return record

    async def create_with_status(
        self, *, author_id: int, author_name: str, chat_id: int, thread_id: int | None, source_message_id: int, schedule: Schedule
    ) -> tuple[Record[Reminder], bool]:
        """Return whether this call created the record, for best-effort acknowledgements."""
        key = self.creation_key(author_id=author_id, chat_id=chat_id, thread_id=thread_id, source_message_id=source_message_id)
        old = await self.get_creation(author_id=author_id, chat_id=chat_id, thread_id=thread_id, source_message_id=source_message_id)
        if old is not None:
            return old, False
        self._future(schedule)
        if not schedule.text.strip():
            raise ReminderError("Добавь, о чём напомнить: /remind in 15m выключить духовку.")
        value = Reminder(
            author_id=author_id,
            author_name=author_name,
            chat_id=chat_id,
            thread_id=thread_id,
            text=schedule.text,
            due_at=schedule.due_at,
            timezone=schedule.timezone,
            recurrence=schedule.recurrence,
        )
        await self._require_recurrence_access(value)
        tx = self._tx(author_id)
        tx.expect_absent("items", key)
        tx.put(self.items, key, value, parent=destination(chat_id, thread_id), status="pending")
        self._schedule(tx, key, "deliver", value.due_at)
        created = True
        try:
            await self._commit(tx)
        except Conflict:
            created = False
        return await self.get(author_id, key, chat_id=chat_id, thread_id=thread_id), created

    async def list(
        self, author_id: int, *, chat_id: int | None = None, thread_id: int | None = None, after: str | None = None, limit: int = 20
    ) -> list[Record[Reminder]]:
        rows = await self.items.list(
            self.scope(author_id), parent=None if chat_id is None else destination(chat_id, thread_id), after=after, limit=limit
        )
        if any(row.value.author_id != author_id for row in rows):
            raise ReminderError("Не удалось проверить владельца напоминания.")
        return rows

    async def get(self, author_id: int, key: str, *, chat_id: int | None = None, thread_id: int | None = None) -> Record[Reminder]:
        row = await self.items.get(self.scope(author_id), key)
        if (
            row is None
            or row.value.author_id != author_id
            or (chat_id is not None and (row.value.chat_id, row.value.thread_id) != (chat_id, thread_id))
        ):
            raise ReminderError("Напоминание не найдено среди твоих напоминаний.")
        return row

    @staticmethod
    def _revision(record: Record[Reminder], expected_etag: str | None) -> None:
        if expected_etag is not None and record.etag != expected_etag:
            raise Conflict()

    async def reschedule(
        self,
        author_id: int,
        key: str,
        schedule: Schedule,
        *,
        expected_etag: str | None = None,
        chat_id: int | None = None,
        thread_id: int | None = None,
    ) -> Record[Reminder]:
        record = await self.get(author_id, key, chat_id=chat_id, thread_id=thread_id)
        self._revision(record, expected_etag)
        if record.value.status != "pending":
            raise ReminderError("Перенести можно ожидающее напоминание. Для неподтверждённой доставки есть /remind retry ID.")
        self._future(schedule)
        value = record.value.model_copy(deep=True)
        value.due_at, value.timezone = schedule.due_at, schedule.timezone
        if "recurrence" in schedule.model_fields_set:
            value.recurrence = schedule.recurrence
        if schedule.text.strip():
            value.text = schedule.text
        value.attempts = 0
        await self._require_recurrence_access(value)
        tx = self._tx(author_id)
        self._put(tx, record, value)
        self._schedule(tx, key, "deliver", value.due_at)
        await self._commit(tx)
        return await self.get(author_id, key)

    async def cancel(
        self, author_id: int, key: str, *, expected_etag: str | None = None, chat_id: int | None = None, thread_id: int | None = None
    ) -> Record[Reminder]:
        record = await self.get(author_id, key, chat_id=chat_id, thread_id=thread_id)
        self._revision(record, expected_etag)
        if record.value.status == "cancelled":
            return record
        if record.value.status not in {"pending", "failed", "uncertain"}:
            raise ReminderError("Напоминание уже отправляется или отправлено; отменить его поздно.")
        await self._terminal(record, "cancelled")
        return await self.get(author_id, key)

    async def retry(
        self, author_id: int, key: str, *, expected_etag: str | None = None, chat_id: int | None = None, thread_id: int | None = None
    ) -> Record[Reminder]:
        record = await self.get(author_id, key, chat_id=chat_id, thread_id=thread_id)
        self._revision(record, expected_etag)
        if record.value.status not in {"uncertain", "failed"}:
            raise ReminderError("Повтор нужен только после ошибки или неподтверждённой доставки.")
        await self._require_recurrence_access(record.value)
        value = record.value.model_copy(deep=True)
        value.status, value.attempts = "pending", 0
        value.terminal_at = value.sending_at = value.failure = None
        tx = self._tx(author_id)
        self._put(tx, record, value)
        self._schedule(tx, key, "deliver", self.clock())
        tx.cancel_job(f"cleanup:{key}")
        tx.cancel_job(f"reconcile:{key}")
        await self._commit(tx)
        return await self.get(author_id, key)

    async def _terminal(
        self, record: Record[Reminder], status: ReminderStatus, *, message_id: int | None = None, failure: Failure | None = None
    ) -> None:
        value = record.value.model_copy(deep=True)
        value.status = status
        value.failure = failure
        value.terminal_at = self.clock()
        if status == "delivered":
            value.delivered_at, value.delivered_message_id = self.clock(), message_id
        tx = self._tx(value.author_id)
        self._put(tx, record, value)
        tx.cancel_job(f"deliver:{record.key}")
        tx.cancel_job(f"reconcile:{record.key}")
        self._schedule(tx, record.key, "cleanup", self.clock() + TERMINAL_RETENTION)
        await self._commit(tx)

    async def _delivered(self, record: Record[Reminder], message_id: int) -> None:
        if record.value.recurrence is None:
            await self._terminal(record, "delivered", message_id=message_id)
            return
        value = record.value.model_copy(deep=True)
        assert value.recurrence is not None
        try:
            value.due_at, skipped = next_occurrence(value.due_at, value.timezone, value.recurrence, self.clock())
        except ReminderError:
            await self._terminal(record, "delivered", message_id=message_id)
            return
        value.status, value.sending_at, value.terminal_at, value.failure = "pending", None, None, None
        value.delivered_at, value.delivered_message_id = self.clock(), message_id
        value.attempts = 0
        value.occurrences += 1
        value.skipped_occurrences += skipped
        tx = self._tx(value.author_id)
        self._put(tx, record, value)
        tx.cancel_job(f"reconcile:{record.key}")
        self._schedule(tx, record.key, "deliver", value.due_at)
        await self._commit(tx)

    async def _deliver(self, context: JobContext) -> None:
        try:
            await self._delivery(context)
        except RepositoryUnavailable, TimeoutError, Conflict, TelegramAPIError:
            # An already committed sending marker prevents another network send.
            raise JobRetry("Reminder state can safely be reconciled") from None

    async def _delivery(self, context: JobContext) -> None:
        record = await self.items.get(context.job.scope, context.job.record.key)
        if record is None or not await context.current():
            return
        if record.value.status == "sending":
            await self._terminal(record, "uncertain", failure="uncertain")
            return
        if record.value.status != "pending":
            return
        if record.value.recurrence is not None and not await self._recurring_membership(record.value):
            await self._terminal(record, "failed", failure="rejected")
            return
        value = record.value.model_copy(deep=True)
        value.status, value.sending_at = "sending", self.clock()
        value.attempts += 1
        tx = self._tx(value.author_id)
        self._put(tx, record, value)
        self._schedule(tx, record.key, "reconcile", self.clock() + timedelta(seconds=SEND_TIMEOUT + self.worker.lease_seconds + 5))
        await self._commit(tx)
        sending = await self.get(value.author_id, record.key)
        if sending.value.status != "sending" or not await context.current():
            return
        late = self.clock() > value.due_at + timedelta(minutes=1)
        body = Text(
            "⏰ Напоминание с опозданием" if late else "⏰ Напоминание",
            " для ",
            TextLink(compact(value.author_name, 48) or "тебя", url=f"tg://user?id={value.author_id}"),
            f"\n{value.text}\n\nБыло назначено: {when(value)}" if late else f"\n{value.text}",
        )
        text, entities = body.render()
        method = SendMessage(
            chat_id=value.chat_id,
            message_thread_id=value.thread_id,
            text=text,
            entities=entities,
            parse_mode=None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        try:
            async with asyncio.timeout(SEND_TIMEOUT):
                delivered = await self.bot(method, request_timeout=SEND_TIMEOUT)
        except TelegramRetryAfter as error:
            if value.attempts >= MAX_DELIVERY_ATTEMPTS or error.retry_after > 86400:
                await self._terminal(sending, "failed", failure="rate_limit")
                return
            changed = sending.value.model_copy(deep=True)
            changed.status, changed.sending_at = "pending", None
            tx = self._tx(changed.author_id)
            self._put(tx, sending, changed)
            self._schedule(tx, record.key, "deliver", self.clock() + timedelta(seconds=max(1, error.retry_after)))
            tx.cancel_job(f"reconcile:{record.key}")
            await self._commit(tx)
            return
        except TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramUnauthorizedError:
            await self._terminal(sending, "failed", failure="rejected")
            return
        except Exception:
            await self._terminal(sending, "uncertain", failure="uncertain")
            return
        await self._delivered(sending, delivered.message_id)

    async def _require_recurrence_access(self, value: Reminder) -> None:
        if value.recurrence is not None and not await self._recurring_membership(value):
            raise ReminderError("Для повторений в чате бот должен быть администратором, а ты — участником с правом писать.")

    async def _recurring_membership(self, value: Reminder) -> bool:
        if value.chat_id > 0:
            return value.chat_id == value.author_id
        try:
            async with asyncio.timeout(10):
                me = await self.bot.get_chat_member(value.chat_id, self.bot.id)
                if not isinstance(me, ADMINS):
                    return False
                member = await self.bot.get_chat_member(value.chat_id, value.author_id)
        except TelegramBadRequest, TelegramForbiddenError, TelegramNotFound:
            return False
        return isinstance(member, MEMBERS) and not (
            isinstance(member, ChatMemberRestricted) and (not member.is_member or not member.can_send_messages)
        )

    async def _reconcile(self, context: JobContext) -> None:
        try:
            record = await self.items.get(context.job.scope, context.job.record.key)
            if record is not None and record.value.status == "sending" and await context.current():
                await self._terminal(record, "uncertain", failure="uncertain")
        except RepositoryUnavailable, TimeoutError, Conflict:
            raise JobRetry("Reminder delivery state needs reconciliation") from None

    async def _cleanup(self, context: JobContext) -> None:
        try:
            record = await self.items.get(context.job.scope, context.job.record.key)
            if record is None or not await context.current():
                return
            if record.value.terminal_at is None or record.value.status in {"pending", "sending"}:
                raise JobHold("Reminder cleanup requires terminal state")
            if self.clock() < record.value.terminal_at + TERMINAL_RETENTION:
                raise JobRetry("Reminder retention window remains open")
            tx = self._tx(record.value.author_id)
            tx.delete(record)
            for kind in ("deliver", "reconcile", "cleanup"):
                tx.cancel_job(f"{kind}:{record.key}")
            await self._commit(tx)
        except RepositoryUnavailable, TimeoutError, Conflict:
            raise JobRetry("Reminder cleanup is safe to retry") from None

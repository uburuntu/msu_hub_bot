"""Owned feedback drafts and one-request delivery through the durable worker."""

import asyncio
import hashlib
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.types import InlineKeyboardButton, Message
from pydantic import ValidationError

from msu_hub_bot.storage.errors import RepositoryUnavailable
from msu_hub_bot.storage.features import (
    Conflict,
    FeatureStore,
    FeatureWorker,
    InvalidPayload,
    JobContext,
    JobHold,
    JobRetry,
    Record,
    RecordKey,
    Scope,
    Transaction,
)

from .models import (
    FeedbackAccessDenied,
    FeedbackActivity,
    FeedbackContext,
    FeedbackCreation,
    FeedbackDiagnostic,
    FeedbackDraft,
    FeedbackError,
    FeedbackFailure,
    FeedbackKind,
    FeedbackMessage,
    FeedbackNotFound,
    FeedbackOrigin,
    FeedbackReport,
    FeedbackReview,
    FeedbackReviewStatus,
    FeedbackSelection,
    FeedbackStatus,
    SelectedFeedbackContext,
)

DRAFT_RETENTION = timedelta(hours=24)
SUBMISSION_WINDOW = timedelta(hours=1)
MAX_SUBMISSIONS = 5
MAX_DRAFT_CREATIONS = 50
SEND_TIMEOUT = 15
MAX_DELIVERY_ATTEMPTS = 8
MAX_CONFLICT_RETRIES = 4
SCOPE = Scope("inbox")


def _review_key(submitted_at: datetime, report_id: str) -> str:
    elapsed = submitted_at.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    milliseconds = elapsed.days * 86_400_000 + elapsed.seconds * 1000 + elapsed.microseconds // 1000
    if not 0 <= milliseconds <= 9_999_999_999_999_999:
        raise InvalidPayload()
    return f"{9_999_999_999_999_999 - milliseconds:016d}:{report_id}"


def _ui_digest(author_id: int, key: str, chat_id: int, message_id: int) -> str:
    return hashlib.sha256(f"{author_id}:{key}:{chat_id}:{message_id}".encode()).hexdigest()


def _preview_digest(report: FeedbackReport) -> str:
    return hashlib.sha256(report.model_dump_json().encode("utf-8")).hexdigest()


def _selected(context: FeedbackContext, selection: FeedbackSelection) -> SelectedFeedbackContext:
    # A new submission copies only named, previewed fields. Unknown draft extras
    # survive draft edits but must not silently become a permanent attachment.
    def message(value: FeedbackMessage | None) -> FeedbackMessage | None:
        return None if value is None else FeedbackMessage.model_validate(value.model_dump(include=set(FeedbackMessage.model_fields)))

    origin = context.origin
    return SelectedFeedbackContext(
        origin=FeedbackOrigin.model_validate(origin.model_dump(include=set(FeedbackOrigin.model_fields))) if selection.chat else None,
        reply=message(context.reply) if selection.reply else None,
        recent_messages=[item for value in context.recent_messages if (item := message(value)) is not None] if selection.recent else [],
        diagnostics=[
            FeedbackDiagnostic.model_validate(value.model_dump(include=set(FeedbackDiagnostic.model_fields)))
            for value in context.diagnostics
        ]
        if selection.diagnostics
        else [],
        recent_available=context.recent_available if selection.recent else True,
        reply_available=context.reply_available if selection.reply else True,
        diagnostics_since=context.diagnostics_since if selection.diagnostics else None,
    )


class FeedbackService:
    def __init__(
        self,
        bot: Bot,
        store: FeatureStore,
        worker: FeatureWorker,
        *,
        destination_chat_id: int,
        destination_name: str = "Event Tracking",
        reviewer_ids: Iterable[int] = (),
        review_button: Callable[[str], InlineKeyboardButton | None] | None = None,
    ) -> None:
        if type(destination_chat_id) is not int or not -(2**63) <= destination_chat_id < 2**63:
            raise ValueError("Invalid feedback destination")
        self.bot, self.store, self.worker = bot, store, worker
        self.destination_chat_id = destination_chat_id
        if not isinstance(destination_name, str) or not destination_name.strip() or len(destination_name) > 100:
            raise ValueError("Invalid feedback destination name")
        self.destination_name = destination_name
        reviewers = tuple(reviewer_ids)
        if any(type(user_id) is not int or not 0 < user_id < 2**63 for user_id in reviewers):
            raise ValueError("Invalid feedback reviewer identity")
        self.reviewer_ids = frozenset(reviewers)
        self.review_button = review_button
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.drafts = store.collection("feedback", "drafts", FeedbackDraft, retention=DRAFT_RETENTION, version=1)
        self.reports = store.collection("feedback", "reports", FeedbackReport, retention=None, version=1)
        self.activity = store.collection("feedback", "activity", FeedbackActivity, retention=DRAFT_RETENTION, version=1)
        self.review_index = store.collection("feedback", "review_index", FeedbackReview, retention=None, version=1)
        worker.register("feedback", "deliver", self._deliver, max_attempts=1024)
        worker.register("feedback", "reconcile", self._reconcile, max_attempts=1024)

    @staticmethod
    def scope(author_id: int) -> Scope:
        if type(author_id) is not int or not 0 < author_id < 2**63:
            raise FeedbackError("Обратную связь можно отправить от личного аккаунта.")
        return SCOPE

    def _tx(self, author_id: int) -> Transaction:
        return self.store.transaction("feedback", self.scope(author_id), operation_id=uuid4().hex)

    @staticmethod
    async def _commit(tx: Transaction) -> None:
        try:
            await tx.commit()
        except RepositoryUnavailable, TimeoutError:
            # The request is frozen by Transaction: a lost response must not
            # rebuild the mutation or choose a different idempotency key.
            await tx.commit()

    def creation_key(self, *, author_id: int, chat_id: int, thread_id: int | None, source_message_id: int) -> str:
        return hashlib.blake2s(f"{self.bot.id}:{author_id}:{chat_id}:{thread_id}:{source_message_id}".encode(), digest_size=8).hexdigest()

    async def _draft(self, author_id: int, key: str) -> Record[FeedbackDraft]:
        record = await self.drafts.get(self.scope(author_id), key)
        if record is None or record.value.author_id != author_id or record.value.expires_at <= self.clock():
            raise FeedbackError("Черновик не найден или устарел. Создай новый через /feedback.")
        return record

    async def _activity(self, author_id: int) -> tuple[Record[FeedbackActivity] | None, FeedbackActivity]:
        record = await self.activity.get(self.scope(author_id), f"user:{author_id}")
        if record is not None and record.value.author_id != author_id:
            raise InvalidPayload()
        value = FeedbackActivity(author_id=author_id) if record is None else record.value.model_copy(deep=True)
        value.submissions = [stamp for stamp in value.submissions if stamp > self.clock() - SUBMISSION_WINDOW]
        value.creations = [item for item in value.creations if item.expires_at > self.clock()]
        return record, value

    def _put_activity(self, tx: Transaction, record: Record[FeedbackActivity] | None, value: FeedbackActivity) -> None:
        key = f"user:{value.author_id}"
        if record is None:
            tx.expect_absent("activity", key)
        else:
            tx.expect(record)
        tx.put(self.activity, key, value, expires_at=self.clock() + DRAFT_RETENTION)

    @staticmethod
    def _rate(value: FeedbackActivity) -> None:
        if len(value.submissions) >= MAX_SUBMISSIONS:
            raise FeedbackError("За последний час уже отправлено пять отзывов. Попробуй немного позже.")

    @staticmethod
    def _creation_allowed(value: FeedbackActivity, key: str, chat_id: int, source_message_id: int) -> None:
        if any(item.key == key or (item.chat_id == chat_id and item.source_message_id >= source_message_id) for item in value.creations):
            raise FeedbackError("Это старый запрос: черновик уже заменён или отменён. Отправь новое сообщение с /feedback.")
        if len(value.creations) >= MAX_DRAFT_CREATIONS:
            raise FeedbackError("За последние сутки уже открыто 50 черновиков. Попробуй немного позже.")

    async def create(
        self,
        *,
        author_id: int,
        author_name: str,
        chat_id: int,
        thread_id: int | None,
        source_message_id: int,
        description: str,
        candidates: FeedbackContext,
        kind: FeedbackKind = "bug",
    ) -> Record[FeedbackDraft]:
        self.scope(author_id)
        if not self.destination_chat_id:
            raise FeedbackError("Обратная связь пока не настроена. Попробуй позже.")
        if not description.strip():
            raise FeedbackError("Напиши, что случилось или что хочется добавить: /feedback описание.")
        if len(description) > 2000:
            raise FeedbackError("Описание слишком длинное: оставь до 2000 символов.")
        try:
            description.encode("utf-8")
            value = FeedbackDraft(
                author_id=author_id,
                author_name=author_name,
                chat_id=chat_id,
                thread_id=thread_id,
                source_message_id=source_message_id,
                description=description,
                created_at=self.clock(),
                expires_at=self.clock() + DRAFT_RETENTION,
                destination_chat_id=self.destination_chat_id,
                destination_name=self.destination_name,
                context=candidates.model_copy(deep=True),
                kind=kind,
                selection=FeedbackSelection(reply=candidates.reply is not None, diagnostics=bool(candidates.diagnostics)),
            )
        except ValidationError, UnicodeError:
            raise FeedbackError("Не удалось подготовить ограниченный контекст отзыва. Попробуй ещё раз.") from None
        key = self.creation_key(author_id=author_id, chat_id=chat_id, thread_id=thread_id, source_message_id=source_message_id)
        for _ in range(MAX_CONFLICT_RETRIES):
            existing = await self.drafts.get(self.scope(author_id), key)
            if existing is not None:
                if existing.value.author_id != author_id:
                    raise InvalidPayload()
                return existing
            saved = await self.reports.get(self.scope(author_id), key)
            if saved is not None:
                if (saved.value.author_id, saved.value.report_id) != (author_id, key):
                    raise InvalidPayload()
                raise FeedbackError("Этот отзыв уже сохранён для отправки.")
            activity, owner = await self._activity(author_id)
            self._rate(owner)
            self._creation_allowed(owner, key, chat_id, source_message_id)
            previous = None if owner.active_draft is None else await self.drafts.get(self.scope(author_id), owner.active_draft)
            tx = self._tx(author_id)
            if previous is not None:
                if previous.value.author_id != author_id:
                    raise InvalidPayload()
                tx.delete(previous)
            tx.expect_absent("drafts", key)
            tx.expect_absent("reports", key)
            tx.put(self.drafts, key, value, expires_at=value.expires_at)
            owner.active_draft, owner.active_until = key, value.expires_at
            owner.creations = [
                *owner.creations,
                FeedbackCreation(key=key, chat_id=chat_id, source_message_id=source_message_id, expires_at=value.expires_at),
            ]
            self._put_activity(tx, activity, owner)
            try:
                await self._commit(tx)
            except Conflict:
                continue
            return await self._draft(author_id, key)
        raise Conflict()

    async def bind(self, author_id: int, key: str, *, chat_id: int, message_id: int, expected_etag: str) -> Record[FeedbackDraft]:
        record = await self._draft(author_id, key)
        if record.value.ui_chat_id is not None:
            if (record.value.ui_chat_id, record.value.ui_message_id) != (chat_id, message_id):
                raise FeedbackError("У этого черновика уже есть своя карточка.")
            return record
        self._revision(record, expected_etag)
        if chat_id not in {record.value.chat_id, author_id} or type(message_id) is not int or message_id <= 0:
            raise FeedbackError("Не удалось проверить карточку отзыва.")
        value = record.value.model_copy(update={"ui_chat_id": chat_id, "ui_message_id": message_id}, deep=True)
        return await self._write_draft(record, value)

    async def get(self, author_id: int, key: str, *, ui_chat_id: int, ui_message_id: int) -> Record[FeedbackDraft]:
        record = await self._draft(author_id, key)
        if (record.value.ui_chat_id, record.value.ui_message_id) != (ui_chat_id, ui_message_id):
            raise FeedbackError("Эта кнопка относится к другой карточке отзыва.")
        return record

    @staticmethod
    def _revision(record: Record[FeedbackDraft], expected_etag: str) -> None:
        if record.etag != expected_etag:
            raise Conflict()

    async def _write_draft(self, record: Record[FeedbackDraft], value: FeedbackDraft) -> Record[FeedbackDraft]:
        tx = self._tx(value.author_id)
        tx.expect(record)
        tx.put(self.drafts, record.key, value, expires_at=value.expires_at)
        await self._commit(tx)
        return await self._draft(value.author_id, record.key)

    async def change(
        self,
        author_id: int,
        key: str,
        *,
        expected_etag: str,
        ui_chat_id: int,
        ui_message_id: int,
        kind: FeedbackKind | None = None,
        selection: FeedbackSelection | None = None,
    ) -> Record[FeedbackDraft]:
        record = await self.get(author_id, key, ui_chat_id=ui_chat_id, ui_message_id=ui_message_id)
        self._revision(record, expected_etag)
        value = record.value.model_copy(deep=True)
        if kind is not None:
            value.kind = kind
        if selection is not None:
            value.selection = selection.model_copy(deep=True)
        value.preview_digest = None
        return await self._write_draft(record, value)

    def build_report(self, record: Record[FeedbackDraft]) -> FeedbackReport:
        from .presentation import render_report

        value = record.value
        if value.ui_chat_id is None or value.ui_message_id is None or value.expires_at <= self.clock():
            raise FeedbackError("Сначала открой действующую карточку отзыва.")
        report = FeedbackReport(
            report_id=record.key,
            author_id=value.author_id,
            author_name=value.author_name,
            created_at=value.created_at,
            description=value.description,
            kind=value.kind,
            context=_selected(value.context, value.selection),
            destination_chat_id=value.destination_chat_id,
            destination_name=value.destination_name,
            ui_digest=_ui_digest(value.author_id, record.key, value.ui_chat_id, value.ui_message_id),
        )
        messages = [*report.context.recent_messages, *([report.context.reply] if report.context.reply is not None else [])]
        if any(message.sent_at <= self.clock() - timedelta(days=30) for message in messages):
            raise FeedbackError("Часть выбранных сообщений старше 30 дней. Убери этот контекст и открой предпросмотр снова.")
        try:
            report.rendered_text = render_report(report)
        except ValidationError, UnicodeError:
            raise FeedbackError("Отчёт получился слишком большим. Убери часть контекста.") from None
        if not report.rendered_text.strip():
            raise FeedbackError("Не удалось подготовить текст отчёта.")
        return report

    async def preview(self, author_id: int, key: str, *, expected_etag: str, ui_chat_id: int, ui_message_id: int) -> Record[FeedbackDraft]:
        """Confirm only after the adapter successfully displays build_report()."""
        record = await self.get(author_id, key, ui_chat_id=ui_chat_id, ui_message_id=ui_message_id)
        self._revision(record, expected_etag)
        value = record.value.model_copy(update={"preview_digest": _preview_digest(self.build_report(record))}, deep=True)
        return await self._write_draft(record, value)

    async def get_report(self, author_id: int, key: str) -> Record[FeedbackReport]:
        record = await self.reports.get(self.scope(author_id), key)
        if record is None or (record.value.author_id, record.value.report_id) != (author_id, key):
            raise FeedbackError("Отзыв не найден среди твоих отзывов.")
        return record

    def is_reviewer(self, user_id: int) -> bool:
        return type(user_id) is int and user_id in self.reviewer_ids

    def _require_reviewer(self, user_id: int) -> None:
        if not self.is_reviewer(user_id):
            raise FeedbackAccessDenied()

    @staticmethod
    def _check_review(record: Record[FeedbackReview], report: Record[FeedbackReport] | None = None) -> None:
        value = record.value
        if record.key != _review_key(value.submitted_at, value.report_id) or (record.parent, record.status) != (value.kind, value.status):
            raise InvalidPayload()
        if report is not None and (
            (value.report_id, value.author_id, value.kind, value.created_at, value.submitted_at)
            != (report.key, report.value.author_id, report.value.kind, report.value.created_at, report.value.submitted_at)
            or report.value.review_key != record.key
        ):
            raise InvalidPayload()

    async def review_list(
        self,
        reviewer_id: int,
        *,
        status: FeedbackReviewStatus | None = None,
        kind: FeedbackKind | None = None,
        after: str | None = None,
        limit: int = 20,
    ) -> list[Record[FeedbackReview]]:
        self._require_reviewer(reviewer_id)
        if (
            type(limit) is not int
            or not 1 <= limit <= 50
            or (status is not None and status not in ("new", "in_progress", "done", "dismissed"))
            or (kind is not None and kind not in ("bug", "idea", "other"))
            or (after is not None and re.fullmatch(r"[0-9]{16}:[a-f0-9]{16}", after) is None)
        ):
            raise FeedbackError("Неверные параметры списка отзывов.")
        records = await self.review_index.list(SCOPE, parent=kind, status=status, after=after, limit=limit)
        for record in records:
            self._check_review(record)
        return records

    async def review_get(self, reviewer_id: int, report_id: str) -> tuple[Record[FeedbackReport], Record[FeedbackReview]]:
        self._require_reviewer(reviewer_id)
        if re.fullmatch(r"[a-f0-9]{16}", report_id) is None:
            raise FeedbackNotFound()
        report = await self.reports.get(SCOPE, report_id)
        if report is None:
            raise FeedbackNotFound()
        if report.value.report_id != report_id or report.value.review_key is None:
            raise InvalidPayload()
        review = await self.review_index.get(SCOPE, report.value.review_key)
        if review is None:
            raise InvalidPayload()
        self._check_review(review, report)
        return report, review

    async def review_update(
        self,
        reviewer_id: int,
        report_id: str,
        *,
        expected_etag: str,
        status: FeedbackReviewStatus,
        note: str,
    ) -> Record[FeedbackReview]:
        _, record = await self.review_get(reviewer_id, report_id)
        if record.etag != expected_etag:
            raise Conflict()
        try:
            value = FeedbackReview.model_validate(
                {
                    **record.value.model_dump(),
                    "status": status,
                    "note": note,
                    "reviewer_id": reviewer_id,
                    "reviewed_at": self.clock(),
                }
            )
            value.note.encode("utf-8")
        except ValidationError, UnicodeError:
            raise FeedbackError("Выбери статус отзыва и оставь заметку до 2000 символов.") from None
        tx = self._tx(record.value.author_id)
        tx.expect(record)
        tx.put(self.review_index, record.key, value, parent=value.kind, status=value.status, expires_at=None)
        await self._commit(tx)
        updated = await self.review_index.get(SCOPE, record.key)
        if updated is None:
            raise InvalidPayload()
        self._check_review(updated)
        return updated

    async def submit(self, author_id: int, key: str, *, expected_etag: str, ui_chat_id: int, ui_message_id: int) -> Record[FeedbackReport]:
        for _ in range(MAX_CONFLICT_RETRIES):
            saved = await self.reports.get(self.scope(author_id), key)
            if saved is not None:
                if (saved.value.author_id, saved.value.report_id, saved.value.ui_digest) != (
                    author_id,
                    key,
                    _ui_digest(author_id, key, ui_chat_id, ui_message_id),
                ):
                    raise FeedbackError("Эта кнопка относится к другому отзыву.")
                if saved.value.submission_etag != expected_etag:
                    raise Conflict()
                return saved
            try:
                record = await self.get(author_id, key, ui_chat_id=ui_chat_id, ui_message_id=ui_message_id)
            except FeedbackError:
                # A concurrent confirmation may delete the draft between our
                # report lookup and draft lookup. Reconcile the permanent row.
                if await self.reports.get(self.scope(author_id), key) is not None:
                    continue
                raise
            self._revision(record, expected_etag)
            if record.value.preview_digest is None:
                raise FeedbackError("Сначала посмотри предпросмотр отчёта.")
            report = self.build_report(record)
            if _preview_digest(report) != record.value.preview_digest:
                raise FeedbackError("Предпросмотр изменился. Посмотри его ещё раз перед отправкой.")
            activity, owner = await self._activity(author_id)
            self._rate(owner)
            submitted_at = self.clock()
            review_key = _review_key(submitted_at, report.report_id)
            report = report.model_copy(
                update={"submission_etag": expected_etag, "submitted_at": submitted_at, "review_key": review_key}, deep=True
            )
            summary = " ".join(report.description.split())
            review = FeedbackReview(
                report_id=report.report_id,
                author_id=report.author_id,
                author_name=report.author_name,
                kind=report.kind,
                summary=summary if len(summary) <= 240 else summary[:239] + "…",
                created_at=report.created_at,
                submitted_at=submitted_at,
            )
            tx = self._tx(author_id)
            tx.delete(record)
            tx.expect_absent("reports", key)
            tx.put(self.reports, key, report, status="queued", expires_at=None)
            tx.expect_absent("review_index", review_key)
            tx.put(self.review_index, review_key, review, parent=review.kind, status=review.status, expires_at=None)
            self._schedule(tx, key, "deliver", self.clock())
            owner.submissions = [*owner.submissions, self.clock()]
            if owner.active_draft == key:
                owner.active_draft = owner.active_until = None
            self._put_activity(tx, activity, owner)
            try:
                await self._commit(tx)
            except Conflict:
                continue
            return await self.get_report(author_id, key)
        raise Conflict()

    async def cancel(self, author_id: int, key: str, *, expected_etag: str, ui_chat_id: int, ui_message_id: int) -> None:
        record = await self.get(author_id, key, ui_chat_id=ui_chat_id, ui_message_id=ui_message_id)
        self._revision(record, expected_etag)
        activity, owner = await self._activity(author_id)
        tx = self._tx(author_id)
        tx.delete(record)
        if owner.active_draft == key:
            owner.active_draft = owner.active_until = None
        self._put_activity(tx, activity, owner)
        await self._commit(tx)

    @staticmethod
    def _schedule(tx: Transaction, key: str, kind: str, run_at: datetime) -> None:
        tx.schedule(f"{kind}:{key}", kind, record=RecordKey("reports", key), run_at=run_at)

    def _put_report(self, tx: Transaction, record: Record[FeedbackReport], value: FeedbackReport) -> None:
        tx.expect(record)
        tx.put(self.reports, record.key, value, status=value.status, expires_at=None)

    async def _terminal(
        self,
        record: Record[FeedbackReport],
        status: FeedbackStatus,
        *,
        failure: FeedbackFailure | None = None,
        message_id: int | None = None,
    ) -> None:
        value = record.value.model_copy(
            update={
                "status": status,
                "failure": failure,
                "sent_at": self.clock() if status == "sent" else None,
                "delivered_message_id": message_id,
            },
            deep=True,
        )
        tx = self._tx(value.author_id)
        self._put_report(tx, record, value)
        tx.cancel_job(f"deliver:{record.key}")
        tx.cancel_job(f"reconcile:{record.key}")
        await self._commit(tx)

    async def _job_record(self, context: JobContext) -> Record[FeedbackReport] | None:
        job = context.job
        if job.feature != "feedback" or job.record.collection != "reports" or job.key != f"{job.kind}:{job.record.key}":
            raise JobHold()
        record = await self.reports.get(job.scope, job.record.key)
        if record is not None and (job.scope != self.scope(record.value.author_id) or record.value.report_id != record.key):
            raise JobHold()
        return record

    async def _deliver(self, context: JobContext) -> None:
        try:
            await self._delivery(context)
        except RepositoryUnavailable, TimeoutError, Conflict:
            # Once sending is committed, another run reconciles rather than sends.
            raise JobRetry() from None

    async def _delivery(self, context: JobContext) -> None:
        from .presentation import notification_method

        record = await self._job_record(context)
        if record is None or not await context.current():
            return
        if record.value.status == "sending":
            await self._terminal(record, "uncertain", failure="uncertain")
            return
        if record.value.status != "queued":
            return
        button = self.review_button(record.key) if self.review_button is not None else None
        method = notification_method(record.value, button=button)
        value = record.value.model_copy(
            update={"status": "sending", "sending_at": self.clock(), "attempts": record.value.attempts + 1}, deep=True
        )
        tx = self._tx(value.author_id)
        self._put_report(tx, record, value)
        self._schedule(tx, record.key, "reconcile", self.clock() + timedelta(seconds=SEND_TIMEOUT + self.worker.lease_seconds + 5))
        await self._commit(tx)
        sending = await self.get_report(value.author_id, record.key)
        if sending.value.status != "sending" or not await context.current():
            return
        try:
            async with asyncio.timeout(SEND_TIMEOUT):
                delivered = await self.bot(method, request_timeout=SEND_TIMEOUT)
        except TelegramRetryAfter as error:
            if value.attempts >= MAX_DELIVERY_ATTEMPTS or error.retry_after > 86400:
                await self._terminal(sending, "failed", failure="rate_limit")
                return
            pending = sending.value.model_copy(update={"status": "queued", "sending_at": None}, deep=True)
            tx = self._tx(value.author_id)
            self._put_report(tx, sending, pending)
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
        if not isinstance(delivered, Message) or delivered.chat.id != value.destination_chat_id:
            await self._terminal(sending, "uncertain", failure="uncertain")
            return
        await self._terminal(sending, "sent", message_id=delivered.message_id)

    async def _reconcile(self, context: JobContext) -> None:
        try:
            record = await self._job_record(context)
            if record is not None and record.value.status == "sending" and await context.current():
                await self._terminal(record, "uncertain", failure="uncertain")
        except RepositoryUnavailable, TimeoutError, Conflict:
            raise JobRetry() from None

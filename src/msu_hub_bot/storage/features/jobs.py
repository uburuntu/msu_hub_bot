"""Bounded leased work; delivery semantics remain explicit in each job handler."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from msu_hub_bot.telemetry import Backend, Boundary, JobGaugeName, Outcome, Telemetry

from .models import FeatureError, FeatureProtocolError, Job, identifier, timestamp
from .store import FeatureStore

logger = logging.getLogger(__name__)


class JobHold(FeatureError):
    """Uncertain or unrecoverable work needs reconciliation, not automatic replay."""


class JobExpired(FeatureError):
    """The feature's semantic retry window has ended."""


class JobRetry(FeatureError):
    """A handler explicitly confirms that replaying the whole job is safe."""


class LeaseLost(FeatureError):
    def __init__(self) -> None:
        super().__init__("Feature job lease is no longer current")


class JobOverview(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)
    feature: str
    kind: str
    pending: int = Field(ge=0)
    held: int = Field(ge=0)
    leased: int = Field(ge=0)
    overdue: int = Field(ge=0)
    oldest_due_seconds: float = Field(ge=0, allow_inf_nan=False)

    _identity = field_validator("feature", "kind")(identifier)


type JobHandler = Callable[[JobContext], Awaitable[None]]
type JobAction = Literal["check", "renew", "complete", "retry", "hold", "expire"]


class JobContext:
    def __init__(self, store: FeatureStore, job: Job, *, lease_seconds: int = 60, telemetry: Telemetry | None = None) -> None:
        self.store, self.job, self.lease_seconds = store, job, lease_seconds
        self.telemetry = telemetry

    async def status(self, action: JobAction, *, run_at: datetime | None = None) -> bool:
        request: dict[str, JsonValue] = {
            "feature": self.job.feature,
            "scope": self.job.scope.model_dump(mode="json"),
            "key": self.job.key,
            "generation": self.job.generation,
            "lease_token": self.job.lease_token,
            "action": action,
        }
        if action == "renew":
            request["lease_seconds"] = self.lease_seconds
        if run_at is not None:
            request["run_at"] = timestamp(run_at)
        result = await self.store.backend.feature_request("job_status", request)
        if not isinstance(result, dict) or type(result.get("current")) is not bool:
            raise FeatureProtocolError()
        if result["current"] and self.telemetry is not None:
            self.telemetry.feature_job_transition(self.job.feature, self.job.kind, action)
        return bool(result["current"])

    async def current(self) -> bool:
        return await self.status("check")

    async def renew(self) -> bool:
        return await self.status("renew")


class FeatureWorker:
    """A single owned runner with bounded parallelism and cooperative shutdown."""

    def __init__(
        self,
        store: FeatureStore,
        *,
        telemetry: Telemetry | None = None,
        poll_interval: float = 1.0,
        lease_seconds: int = 60,
        concurrency: int = 4,
        max_attempts: int = 8,
    ) -> None:
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("Feature worker poll interval must be positive")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise ValueError("Feature worker lease must be bounded")
        if type(concurrency) is not int or not 1 <= concurrency <= 20 or type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("Feature worker execution must be bounded")
        self.store, self.telemetry = store, telemetry or Telemetry()
        self.poll_interval, self.lease_seconds = poll_interval, lease_seconds
        self.concurrency, self.max_attempts = concurrency, max_attempts
        self._handlers: dict[tuple[str, str], JobHandler] = {}
        self._attempt_limits: dict[tuple[str, str], int] = {}
        self._stopped = asyncio.Event()
        self._run_lock = asyncio.Lock()

    def register(self, feature: str, kind: str, handler: JobHandler, *, max_attempts: int | None = None) -> None:
        identity = (identifier(feature), identifier(kind))
        if identity in self._handlers or len(self._handlers) >= 64:
            raise ValueError("Feature job registration must be unique and bounded")
        if max_attempts is not None and (type(max_attempts) is not int or not 1 <= max_attempts <= 10000):
            raise ValueError("Feature retry attempts must be bounded")
        self._handlers[identity] = handler
        self._attempt_limits[identity] = self.max_attempts if max_attempts is None else max_attempts
        self.telemetry.register_feature_job(*identity)

    def stop(self) -> None:
        self._stopped.set()

    async def _renew(self, context: JobContext) -> None:
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            if not await context.renew():
                raise LeaseLost()

    async def _execute(self, context: JobContext, handler: JobHandler) -> None:
        # A renewal failure cancels local work. Fencing cannot recall an HTTP request
        # already accepted by another service; handlers must reconcile uncertainty.
        async def invoke() -> None:
            with self.telemetry.operation(
                Boundary.JOB,
                "feature.job",
                backend=Backend.SUPABASE,
                trace=False,
                feature=context.job.feature,
                job_kind=context.job.kind,
                attempt=context.job.attempts,
            ) as operation:
                try:
                    await handler(context)
                except JobExpired:
                    operation.set_outcome(Outcome.IGNORED)
                    raise
                except JobRetry, JobHold:
                    operation.set_outcome(Outcome.UNAVAILABLE)
                    raise

        task = asyncio.create_task(invoke(), name="feature-job")
        renewal = asyncio.create_task(self._renew(context), name="feature-lease")
        try:
            done, _ = await asyncio.wait({task, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
            await task
        finally:
            task.cancel()
            renewal.cancel()
            await asyncio.gather(task, renewal, return_exceptions=True)

    async def _handle(self, job: Job) -> None:
        context = JobContext(self.store, job, lease_seconds=self.lease_seconds, telemetry=self.telemetry)
        handler = self._handlers.get((job.feature, job.kind))
        if handler is None:
            raise FeatureProtocolError()
        if not await context.current():
            # Release only our old claim; the backend preserves a newer generation.
            await context.status("complete")
            return
        if job.retry_until is not None and datetime.now(UTC) >= job.retry_until:
            await context.status("expire")
            return
        try:
            await self._execute(context, handler)
        except asyncio.CancelledError:
            # A killed worker leaves the claim to expire, never acknowledges success.
            raise
        except JobExpired:
            await context.status("expire")
        except JobRetry:
            if job.attempts >= self._attempt_limits[(job.feature, job.kind)]:
                await context.status("hold")
            else:
                retry = datetime.now(UTC) + timedelta(seconds=min(300, 2 ** min(job.attempts, 8)))
                await context.status("retry", run_at=retry)
        except LeaseLost:
            await context.status("complete")
        except JobHold:
            await context.status("hold")
        except Exception:
            # Unexpected exceptions may follow an external side effect; retries
            # require explicit JobRetry from a handler that can safely replay.
            logger.warning("Feature job held after an unexpected failure")
            await context.status("hold")
        else:
            await context.status("complete")

    async def run_once(self) -> int:
        async with self._run_lock:
            if self._stopped.is_set() or not self._handlers:
                return 0
            request: dict[str, JsonValue] = {
                "handlers": [{"feature": feature, "kind": kind} for feature, kind in self._handlers],
                "limit": self.concurrency,
                "lease_seconds": self.lease_seconds,
            }
            value = await self.store.backend.feature_request("claim_jobs", request)
            if self._stopped.is_set():
                return 0
            if not isinstance(value, list) or len(value) > self.concurrency:
                raise FeatureProtocolError()
            try:
                jobs = [Job.model_validate(item) for item in value]
            except ValidationError, TypeError, ValueError:
                raise FeatureProtocolError() from None
            if any((job.feature, job.kind) not in self._handlers for job in jobs):
                raise FeatureProtocolError()
            identities = [(job.feature, job.scope, job.key) for job in jobs]
            if len(set(identities)) != len(identities):
                raise FeatureProtocolError()
            results = await asyncio.gather(*(self._handle(job) for job in jobs), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return len(jobs)

    async def observe_once(self) -> None:
        """Separate, bounded read; a monitoring outage must not stall delivery."""
        if not self._handlers:
            return
        async with asyncio.timeout(5):
            raw = await self.store.backend.feature_request(
                "job_overview", {"handlers": [{"feature": feature, "kind": kind} for feature, kind in self._handlers]}
            )
        if not isinstance(raw, list) or len(raw) != len(self._handlers):
            raise FeatureProtocolError()
        try:
            rows = [JobOverview.model_validate(item) for item in raw]
        except ValidationError:
            raise FeatureProtocolError() from None
        values = {
            (row.feature, row.kind): {
                JobGaugeName.PENDING: float(row.pending),
                JobGaugeName.HELD: float(row.held),
                JobGaugeName.LEASED: float(row.leased),
                JobGaugeName.OVERDUE: float(row.overdue),
                JobGaugeName.OLDEST_DUE: row.oldest_due_seconds,
            }
            for row in rows
        }
        if set(values) != set(self._handlers) or any(row.leased + row.overdue > row.pending for row in rows):
            raise FeatureProtocolError()
        self.telemetry.feature_job_snapshot(values)

    async def _observe_loop(self) -> None:
        failed = False
        while not self._stopped.is_set():
            try:
                await self.observe_once()
                if failed:
                    logger.info("Feature queue monitoring recovered")
                failed = False
            except asyncio.CancelledError:
                raise
            except Exception:
                if not failed:
                    logger.warning("Feature queue monitoring unavailable")
                failed = True
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=30)
            except TimeoutError:
                pass

    async def run(self) -> None:
        monitor = asyncio.create_task(self._observe_loop(), name="feature-queue-monitor") if self.telemetry.config.export else None
        try:
            await self._run()
        finally:
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Database exceptions are already redacted and traced at the boundary.
                logger.warning("Feature work scan failed")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass

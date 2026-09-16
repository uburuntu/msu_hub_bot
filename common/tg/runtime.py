"""Application-owned update and background-task admission and shutdown."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

from msu_hub_bot.telemetry import Boundary, GaugeName, Telemetry

ResultT = TypeVar("ResultT")
EventT = TypeVar("EventT")


async def gather_complete(*operations: Awaitable[ResultT]) -> list[ResultT]:
    """Await every child before propagating a batch failure to its owning task."""
    results = await asyncio.gather(*operations, return_exceptions=True)
    completed: list[ResultT] = []
    for result in results:
        if isinstance(result, BaseException):
            raise result
        completed.append(result)
    return completed


class AdmissionClosed(RuntimeError):
    """New background work cannot join the shutting-down application."""


@dataclass(frozen=True, slots=True)
class DrainResult:
    cancelled_updates: int
    cancelled_jobs: int
    failed_jobs: int


class DrainTimeout(TimeoutError):
    """Consumers remain alive; their resources must not be closed yet."""

    def __init__(self, updates: int, jobs: int) -> None:
        self.updates = updates
        self.jobs = jobs
        super().__init__(f"Task drain exceeded its deadline ({updates} updates, {jobs} jobs)")


class Supervisor:
    """Track whole worker tasks, including work after middleware returns.

    Stop recurring producers before draining. Jobs submitted by admitted workers
    and their owned descendants remain accepted during the graceful drain.
    Factories are invoked only after admission, so rejection leaks no coroutine.
    """

    def __init__(self, telemetry: Telemetry | None = None) -> None:
        self.telemetry = telemetry or Telemetry()
        self._updates: set[asyncio.Task[Any]] = set()
        self._jobs: set[asyncio.Task[Any]] = set()
        self._updates_open = True
        self._jobs_open = True
        self._cancelling = False
        self._failed_jobs = 0
        self._drain_lock = asyncio.Lock()

    @property
    def update_count(self) -> int:
        return sum(not task.done() for task in self._updates)

    @property
    def job_count(self) -> int:
        return sum(not task.done() for task in self._jobs)

    @property
    def failed_jobs(self) -> int:
        """Aggregate only; jobs own their user feedback and sanitized diagnostics."""
        return self._failed_jobs

    def admit_current_update(self) -> None:
        if not self._updates_open:
            raise asyncio.CancelledError("Update admission is closed")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Update admission requires an asyncio task")
        if task in self._jobs:
            raise RuntimeError("A background job cannot become a polling worker")
        if task not in self._updates:
            self._updates.add(task)
            self.telemetry.gauge(GaugeName.UPDATES_ACTIVE, len(self._updates))
            task.add_done_callback(self._update_done)

    def close_updates(self) -> None:
        self._updates_open = False

    def create_job(self, factory: Callable[[], Coroutine[Any, Any, ResultT]], *, trace: bool = True) -> asyncio.Task[ResultT]:
        parent = asyncio.current_task()
        owned_parent = parent in self._updates or parent in self._jobs
        if self._cancelling or (not self._updates_open and not owned_parent):
            raise AdmissionClosed("Background job admission is closed")
        if not self._jobs_open and parent not in self._jobs:
            raise AdmissionClosed("Background job admission is closed")

        async def observed_job() -> ResultT:
            with self.telemetry.operation(Boundary.JOB, "background", trace=trace):
                return await factory()

        task = asyncio.create_task(observed_job(), name="bot-background-job", context=self.telemetry.job_context())
        self._jobs.add(task)
        self.telemetry.gauge(GaugeName.JOBS_ACTIVE, len(self._jobs))
        task.add_done_callback(self._job_done)
        return task

    def _update_done(self, task: asyncio.Task[Any]) -> None:
        self._updates.discard(task)
        self.telemetry.gauge(GaugeName.UPDATES_ACTIVE, len(self._updates))
        if not task.cancelled():
            task.exception()  # The error boundary owns reporting, not the event loop.

    def _job_done(self, task: asyncio.Task[Any]) -> None:
        if task not in self._jobs:
            return
        self._jobs.discard(task)
        self.telemetry.gauge(GaugeName.JOBS_ACTIVE, len(self._jobs))
        if not task.cancelled() and task.exception() is not None:
            self._failed_jobs += 1

    @staticmethod
    async def _wait_until(tasks: set[asyncio.Task[Any]], deadline: float) -> None:
        loop = asyncio.get_running_loop()
        while pending := {task for task in tasks if not task.done()}:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)

    async def drain(self, timeout: float = 60, *, cancel_timeout: float = 5) -> DrainResult:
        """Close admission and settle work within one total deadline.

        ``cancel_timeout`` reserves part of ``timeout`` for cancellation cleanup.
        A timeout raises rather than pretending consumers are safe to close.
        The application runner, never an admitted update/job, owns this call.
        """
        if not math.isfinite(timeout) or not math.isfinite(cancel_timeout) or timeout < 0 or cancel_timeout < 0:
            raise ValueError("Drain timeouts must be finite and nonnegative")
        if asyncio.current_task() in self._updates or asyncio.current_task() in self._jobs:
            raise RuntimeError("An owned task cannot drain its own supervisor")
        if self._drain_lock.locked():
            raise RuntimeError("Task drain is already in progress")
        async with self._drain_lock:
            self.close_updates()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            graceful_deadline = deadline - min(timeout, cancel_timeout)
            cancelled_updates: set[asyncio.Task[Any]] = set()
            cancelled_jobs: set[asyncio.Task[Any]] = set()
            try:
                await self._wait_until(self._updates, graceful_deadline)
                self._jobs_open = False
                await self._wait_until(self._jobs, graceful_deadline)
                self._cancelling = True
                cancelled_updates = {task for task in self._updates if not task.done()}
                cancelled_jobs = {task for task in self._jobs if not task.done()}
                pending = cancelled_updates | cancelled_jobs
                for task in pending:
                    task.cancel()
                await self._wait_until(pending, deadline)
            except asyncio.CancelledError:
                self._jobs_open = False
                self._cancelling = True
                for task in self._updates | self._jobs:
                    if not task.done():
                        task.cancel()
                raise
            for task in tuple(self._jobs):
                if task.done():
                    self._job_done(task)
            if self.update_count or self.job_count:
                raise DrainTimeout(self.update_count, self.job_count)
            return DrainResult(len(cancelled_updates), len(cancelled_jobs), self.failed_jobs)


class AdmissionMiddleware:
    """Install before state and service middleware; no aiogram globals required."""

    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor

    async def __call__(
        self,
        handler: Callable[[EventT, dict[str, Any]], Awaitable[ResultT]],
        event: EventT,
        data: dict[str, Any],
    ) -> ResultT:
        self.supervisor.admit_current_update()
        return await handler(event, data)

"""Leased worker orchestration with scripted storage boundaries and no network."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from msu_hub_bot.storage.features.jobs import FeatureWorker, JobContext, JobExpired, JobHold, JobRetry
from msu_hub_bot.storage.features.models import FeatureProtocolError, Job
from msu_hub_bot.storage.features.store import FeatureStore

CANARY = "synthetic-private-job-payload"
TOKEN = str(UUID(int=10))


def raw_job(*, key="round:1", **changes):
    return {
        "feature": "sample",
        "scope": {"key": "chat:-100123", "owner": "bot"},
        "key": key,
        "kind": "finish",
        "record": {"collection": "rounds", "key": key},
        "generation": 7,
        "lease_token": TOKEN,
        "run_at": datetime(2020, 1, 1, tzinfo=UTC).isoformat(),
        "attempts": 1,
        "retry_until": None,
        **changes,
    }


class Backend:
    def __init__(self, claimed=None, *, status=None, claim_gate=None):
        self.claimed = [] if claimed is None else claimed
        self.status = status
        self.claim_gate = claim_gate
        self.claim_started = asyncio.Event()
        self.calls = []

    async def feature_request(self, operation, request):
        self.calls.append((operation, deepcopy(request)))
        if operation == "claim_jobs":
            self.claim_started.set()
            if self.claim_gate is not None:
                await self.claim_gate.wait()
            if isinstance(self.claimed, BaseException):
                raise self.claimed
            return deepcopy(self.claimed)
        assert operation == "job_status", "Unexpected storage operation"
        return {"current": True} if self.status is None else await self.status(request)

    def actions(self, *, key=None):
        return [
            request["action"] for operation, request in self.calls if operation == "job_status" and (key is None or request["key"] == key)
        ]


def configured(claimed=None, *, status=None, **options):
    backend = Backend(claimed, status=status)
    worker = FeatureWorker(FeatureStore(backend), **options)
    return backend, worker


async def test_success_uses_registered_claim_and_original_fencing_identity():
    claimed = raw_job()
    backend, worker = configured([claimed], lease_seconds=90, concurrency=2)
    seen = []

    async def handler(context):
        seen.append(context.job)
        assert await context.current()

    worker.register("sample", "finish", handler)
    assert await worker.run_once() == 1
    assert len(seen) == 1
    assert backend.calls[0] == ("claim_jobs", {"handlers": [{"feature": "sample", "kind": "finish"}], "limit": 2, "lease_seconds": 90})
    assert backend.actions() == ["check", "check", "complete"]
    for operation, request in backend.calls:
        if operation == "job_status":
            assert request["generation"] == 7 and request["lease_token"] == TOKEN
            assert request["feature"] == "sample" and request["scope"] == claimed["scope"]


async def test_obsolete_generation_never_enters_handler_and_releases_only_original_claim():
    async def status(_):
        return {"current": False}

    backend, worker = configured([raw_job()], status=status)
    handler = AsyncMock()
    worker.register("sample", "finish", handler)
    assert await worker.run_once() == 1
    handler.assert_not_awaited()
    assert backend.actions() == ["check", "complete"]
    assert all(
        request["generation"] == 7 and request["lease_token"] == TOKEN for operation, request in backend.calls if operation == "job_status"
    )


async def test_coalesced_work_completion_does_not_adopt_new_generation():
    async def status(request):
        return {"current": request["action"] == "check"}

    backend, worker = configured([raw_job()], status=status)
    worker.register("sample", "finish", AsyncMock())
    assert await worker.run_once() == 1
    assert backend.actions() == ["check", "complete"]
    complete = backend.calls[-1][1]
    assert complete["generation"] == 7 and complete["lease_token"] == TOKEN


@pytest.mark.parametrize(
    ("error", "action"),
    [
        (JobHold(CANARY), "hold"),
        (JobExpired(CANARY), "expire"),
        (RuntimeError(CANARY), "hold"),
        (TimeoutError(CANARY), "hold"),
    ],
)
async def test_failures_require_explicit_retry_and_never_export_exception_payload(error, action, caplog):
    backend, worker = configured([raw_job()])

    async def handler(_):
        raise error

    worker.register("sample", "finish", handler)
    assert await worker.run_once() == 1
    assert backend.actions() == ["check", action]
    assert CANARY not in caplog.text
    assert all(CANARY not in repr(request) for _, request in backend.calls)


async def test_explicit_safe_retry_is_scheduled_with_same_generation_and_bounded_backoff():
    backend, worker = configured([raw_job(attempts=2)])

    async def handler(_):
        raise JobRetry(CANARY)

    worker.register("sample", "finish", handler)
    before = datetime.now(UTC)
    assert await worker.run_once() == 1
    after = datetime.now(UTC)
    assert backend.actions() == ["check", "retry"]
    request = backend.calls[-1][1]
    assert request["generation"] == 7 and request["lease_token"] == TOKEN
    retry_at = datetime.fromisoformat(request["run_at"])
    assert before + timedelta(seconds=4) <= retry_at <= after + timedelta(seconds=4)


async def test_retry_exhaustion_holds_unfinished_work_instead_of_pretending_completion():
    backend, worker = configured([raw_job(attempts=3)], max_attempts=3)

    async def handler(_):
        raise JobRetry()

    worker.register("sample", "finish", handler)
    await worker.run_once()
    assert backend.actions() == ["check", "hold"]


@pytest.mark.parametrize(("attempts", "action"), [(8, "retry"), (1024, "hold")])
async def test_feature_retry_limit_overrides_default_without_becoming_unbounded(attempts, action):
    backend, worker = configured([raw_job(attempts=attempts)], max_attempts=8)

    async def handler(_):
        raise JobRetry()

    worker.register("sample", "finish", handler, max_attempts=1024)
    await worker.run_once()
    assert backend.actions() == ["check", action]


async def test_elapsed_semantic_retry_horizon_expires_without_external_effect():
    backend, worker = configured([raw_job(retry_until=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())])
    handler = AsyncMock()
    worker.register("sample", "finish", handler)
    await worker.run_once()
    handler.assert_not_awaited()
    assert backend.actions() == ["check", "expire"]


async def test_forever_pending_job_has_no_implicit_wall_clock_expiry():
    backend, worker = configured([raw_job(run_at=datetime(2000, 1, 1, tzinfo=UTC).isoformat(), retry_until=None)])
    handler = AsyncMock()
    worker.register("sample", "finish", handler)
    await worker.run_once()
    handler.assert_awaited_once()
    assert backend.actions() == ["check", "complete"]


async def test_shutdown_cancellation_waits_for_handler_cleanup_and_never_acknowledges():
    backend, worker = configured([raw_job()])
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def handler(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    worker.register("sample", "finish", handler)
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cleaned.is_set()
    assert backend.actions() == ["check"]
    assert not [task for task in asyncio.all_tasks() if not task.done() and task.get_name() in {"feature-job", "feature-lease"}]


async def test_lease_renewal_loss_cancels_work_before_releasing_old_claim(monkeypatch):
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def status(request):
        return {"current": request["action"] == "check"}

    backend, worker = configured([raw_job()], status=status)

    async def renew_after_handler_entered(context):
        await entered.wait()
        assert await context.renew() is False
        from msu_hub_bot.storage.features.jobs import LeaseLost

        raise LeaseLost()

    monkeypatch.setattr(worker, "_renew", renew_after_handler_entered)

    async def handler(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    worker.register("sample", "finish", handler)
    assert await asyncio.wait_for(worker.run_once(), 1) == 1
    assert cleaned.is_set()
    assert backend.actions() == ["check", "renew", "complete"]
    assert backend.calls[-1][1]["generation"] == 7


async def test_automatic_renewal_is_owned_and_cancelled_when_handler_finishes(monkeypatch):
    import msu_hub_bot.storage.features.jobs as module

    renewed = asyncio.Event()
    sleep_waiting = asyncio.Event()
    real_sleep = asyncio.sleep

    async def controlled_sleep(delay):
        if delay == 20:
            sleep_waiting.set()
            return
        await real_sleep(delay)

    async def status(request):
        if request["action"] == "renew":
            renewed.set()
            await asyncio.Event().wait()
        return {"current": True}

    backend, worker = configured([raw_job()], status=status, lease_seconds=60)
    monkeypatch.setattr(module.asyncio, "sleep", controlled_sleep)

    async def handler(_):
        await renewed.wait()

    worker.register("sample", "finish", handler)
    assert await asyncio.wait_for(worker.run_once(), 1) == 1
    assert sleep_waiting.is_set()
    assert backend.actions() == ["check", "renew", "complete"]
    assert backend.calls[2][1]["lease_seconds"] == 60
    assert not [task for task in asyncio.all_tasks() if not task.done() and task.get_name() == "feature-lease"]


async def test_independent_jobs_run_concurrently_within_one_bounded_pass():
    backend, worker = configured([raw_job(key="round:1"), raw_job(key="round:2")], concurrency=2)
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = set()

    async def handler(context):
        started.add(context.job.key)
        if len(started) == 2:
            both_started.set()
        await release.wait()

    worker.register("sample", "finish", handler)
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(both_started.wait(), 1)
    assert not running.done()
    release.set()
    assert await running == 2
    assert backend.calls[0][1]["limit"] == 2
    assert backend.actions(key="round:1") == backend.actions(key="round:2") == ["check", "complete"]


async def test_failed_status_for_one_job_does_not_abandon_another_owned_job():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def status(request):
        if request["key"] == "round:1":
            raise RuntimeError("synthetic database outage")
        return {"current": True}

    backend, worker = configured([raw_job(key="round:1"), raw_job(key="round:2")], status=status)

    async def handler(_):
        entered.set()
        await release.wait()

    worker.register("sample", "finish", handler)
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 1)
    assert not running.done()
    release.set()
    with pytest.raises(RuntimeError, match="database outage"):
        await running
    assert backend.actions(key="round:2") == ["check", "complete"]


@pytest.mark.parametrize(
    "claimed",
    [
        {},
        [raw_job(kind="unregistered")],
        [raw_job(generation=True)],
        [raw_job(lease_token=CANARY)],
        [raw_job(), raw_job()],
        [raw_job(), raw_job(generation=8)],
    ],
)
async def test_malformed_or_duplicate_claims_never_enter_handlers(claimed):
    backend, worker = configured(claimed, concurrency=4)
    handler = AsyncMock()
    worker.register("sample", "finish", handler)
    with pytest.raises(FeatureProtocolError):
        await worker.run_once()
    handler.assert_not_awaited()
    assert backend.actions() == []


async def test_overfull_claim_is_rejected_before_any_effect():
    backend, worker = configured([raw_job(key=str(index)) for index in range(3)], concurrency=2)
    handler = AsyncMock()
    worker.register("sample", "finish", handler)
    with pytest.raises(FeatureProtocolError):
        await worker.run_once()
    handler.assert_not_awaited()
    assert backend.actions() == []


async def test_no_handlers_or_stopped_worker_never_claims():
    backend, worker = configured()
    assert await worker.run_once() == 0
    worker.register("sample", "finish", AsyncMock())
    worker.stop()
    assert await worker.run_once() == 0
    await worker.run()
    assert backend.calls == []


async def test_stop_during_claim_does_not_admit_returned_work():
    release = asyncio.Event()
    backend = Backend([raw_job()], claim_gate=release)
    worker = FeatureWorker(FeatureStore(backend))
    handler = AsyncMock()
    worker.register("sample", "finish", handler)
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(backend.claim_started.wait(), 1)
    worker.stop()
    release.set()
    await running
    handler.assert_not_awaited()
    assert backend.actions() == []


async def test_scan_failure_is_redacted_and_runner_can_stop(caplog):
    backend = Backend(RuntimeError(CANARY))
    worker = FeatureWorker(FeatureStore(backend), poll_interval=30)
    worker.register("sample", "finish", AsyncMock())
    running = asyncio.create_task(worker.run())
    await asyncio.wait_for(backend.claim_started.wait(), 1)
    worker.stop()
    await asyncio.wait_for(running, 1)
    assert CANARY not in caplog.text


@pytest.mark.parametrize("response", [None, {}, {"current": 1}, {"current": "true"}, {"current": None}])
async def test_job_status_requires_a_real_boolean(response):
    async def status(_):
        return response

    context = JobContext(FeatureStore(Backend(status=status)), Job.model_validate(raw_job()))
    with pytest.raises(FeatureProtocolError):
        await context.current()


@pytest.mark.parametrize(
    "options",
    [
        {"poll_interval": 0},
        {"poll_interval": float("nan")},
        {"lease_seconds": 4},
        {"lease_seconds": 301},
        {"lease_seconds": True},
        {"concurrency": 0},
        {"concurrency": 21},
        {"concurrency": True},
        {"max_attempts": 0},
    ],
)
def test_worker_configuration_bounds(options):
    with pytest.raises(ValueError):
        FeatureWorker(FeatureStore(Backend()), **options)


def test_registry_does_not_allow_silent_handler_replacement():
    _, worker = configured()
    worker.register("sample", "finish", AsyncMock())
    with pytest.raises(ValueError):
        worker.register("sample", "finish", AsyncMock())

import asyncio

import pytest

from common.tg.runtime import AdmissionClosed, AdmissionMiddleware, DrainTimeout, Supervisor


async def test_worker_remains_owned_after_middleware_returns():
    supervisor = Supervisor()
    middleware = AdmissionMiddleware(supervisor)
    returned = asyncio.Event()
    finish_send = asyncio.Event()

    async def handler(event, data):
        return "telegram-method"

    async def worker():
        assert await middleware(handler, object(), {}) == "telegram-method"
        returned.set()
        await finish_send.wait()

    task = asyncio.create_task(worker())
    await returned.wait()
    assert supervisor.update_count == 1
    drain = asyncio.create_task(supervisor.drain(1, cancel_timeout=0.1))
    await asyncio.sleep(0)
    assert not drain.done()
    finish_send.set()
    await task
    result = await drain
    assert supervisor.update_count == 0
    assert result.cancelled_updates == 0


async def test_late_update_is_cancelled_before_handler():
    supervisor = Supervisor()
    supervisor.close_updates()
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    with pytest.raises(asyncio.CancelledError):
        await AdmissionMiddleware(supervisor)(handler, object(), {})
    assert not called
    assert supervisor.update_count == 0


async def test_admitted_update_can_submit_archive_during_drain():
    supervisor = Supervisor()
    admitted = asyncio.Event()
    submit_archive = asyncio.Event()
    archive_started = asyncio.Event()
    finish_archive = asyncio.Event()

    async def archive():
        archive_started.set()
        await finish_archive.wait()

    async def worker():
        supervisor.admit_current_update()
        admitted.set()
        await submit_archive.wait()
        supervisor.create_job(archive)

    task = asyncio.create_task(worker())
    await admitted.wait()
    supervisor.close_updates()
    drain = asyncio.create_task(supervisor.drain(1, cancel_timeout=0.1))
    submit_archive.set()
    await archive_started.wait()
    await task
    assert not drain.done()
    finish_archive.set()
    result = await drain
    assert result.cancelled_jobs == 0
    assert supervisor.job_count == 0


async def test_owned_job_descendant_cannot_escape_job_drain():
    supervisor = Supervisor()
    submit_child = asyncio.Event()
    child_started = asyncio.Event()
    finish_child = asyncio.Event()

    async def child():
        child_started.set()
        await finish_child.wait()

    async def parent():
        await submit_child.wait()
        supervisor.create_job(child)

    parent_task = supervisor.create_job(parent)
    drain = asyncio.create_task(supervisor.drain(1, cancel_timeout=0.1))
    await asyncio.sleep(0)
    submit_child.set()
    await child_started.wait()
    await parent_task
    assert not drain.done()
    finish_child.set()
    await drain


async def test_closed_admission_does_not_create_a_coroutine():
    supervisor = Supervisor()
    await supervisor.drain(0)
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError("Rejected factory was invoked")

    with pytest.raises(AdmissionClosed):
        supervisor.create_job(factory)
    assert not called


async def test_drain_cancels_workers_and_jobs_and_waits_for_cleanup():
    supervisor = Supervisor()
    entered = asyncio.Event()
    cleanup = []

    async def worker():
        supervisor.admit_current_update()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.append("update")

    async def job():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.append("job")

    task = asyncio.create_task(worker())
    job_task = supervisor.create_job(job)
    await entered.wait()
    result = await supervisor.drain(0.1, cancel_timeout=0.08)
    assert result.cancelled_updates == result.cancelled_jobs == 1
    assert task.cancelled() and job_task.cancelled()
    assert sorted(cleanup) == ["job", "update"]


async def test_uncooperative_task_is_reported_without_unbounded_wait():
    supervisor = Supervisor()
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def worker():
        supervisor.admit_current_update()
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await finish.wait()

    task = asyncio.create_task(worker())
    await entered.wait()
    try:
        with pytest.raises(DrainTimeout) as error:
            await supervisor.drain(0.05, cancel_timeout=0.04)
        assert error.value.updates == 1
        assert error.value.jobs == 0
    finally:
        finish.set()
        await task
    await supervisor.drain(0)


async def test_job_failure_is_consumed_and_counted_without_logging_payload(caplog):
    supervisor = Supervisor()

    async def job():
        raise ValueError("private payload canary")

    task = supervisor.create_job(job)
    with pytest.raises(ValueError):
        await task
    result = await supervisor.drain(1)
    assert result.failed_jobs == 1
    assert "private payload canary" not in caplog.text


async def test_owned_task_cannot_deadlock_by_draining_itself():
    supervisor = Supervisor()

    async def worker():
        supervisor.admit_current_update()
        with pytest.raises(RuntimeError, match="own supervisor"):
            await supervisor.drain(1)

    await asyncio.create_task(worker())
    await supervisor.drain(1)


async def test_drain_accounts_for_finished_job_before_its_callback_runs():
    supervisor = Supervisor()

    async def fail():
        raise ValueError("synthetic failure")

    supervisor.create_job(fail)
    await asyncio.sleep(0)
    result = await supervisor.drain(0)
    assert result.failed_jobs == 1
    await asyncio.sleep(0)
    assert supervisor.failed_jobs == 1


async def test_second_drain_cannot_wait_outside_its_own_deadline():
    supervisor = Supervisor()
    finish = asyncio.Event()
    supervisor.create_job(finish.wait)
    drain = asyncio.create_task(supervisor.drain(1, cancel_timeout=0.1))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="already in progress"):
        await supervisor.drain(0.01)
    finish.set()
    await drain


@pytest.mark.parametrize("timeout,cancel_timeout", [(-1, 0), (1, -1), (float("inf"), 1), (1, float("nan"))])
async def test_invalid_deadlines_are_rejected(timeout, cancel_timeout):
    with pytest.raises(ValueError):
        await Supervisor().drain(timeout, cancel_timeout=cancel_timeout)


async def test_batch_failure_waits_for_every_child_before_owner_finishes():
    from common.tg.runtime import gather_complete

    entered, finish = asyncio.Event(), asyncio.Event()
    failure = ValueError("synthetic failure")

    async def failed():
        raise failure

    async def other():
        entered.set()
        await finish.wait()
        return True

    task = asyncio.create_task(gather_complete(failed(), other()))
    await entered.wait()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(ValueError) as caught:
        await task
    assert caught.value is failure


async def test_cancelled_batch_waits_for_child_cleanup():
    from common.tg.runtime import gather_complete

    entered, cleanup = asyncio.Event(), asyncio.Event()

    async def child():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleanup.set()

    task = asyncio.create_task(gather_complete(child()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup.is_set()

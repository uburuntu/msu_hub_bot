"""Decode actual OTLP payloads and verify privacy, ownership and bounded export."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from opentelemetry import baggage, context, trace
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from msu_hub_bot.telegram.middlewares.telemetry import DispatchTelemetryMiddleware, HandlerTelemetryMiddleware
from msu_hub_bot.telemetry import Boundary, GaugeName, Outcome, Provider, Telemetry, TelemetryConfig
from telegram_helpers import make_message
from telemetry_helpers import Capture, config

CANARY = "SYNTHETIC_PRIVATE_CANARY_123456"


@pytest.fixture(autouse=True)
def local_sdk_capture(monkeypatch):
    # The runner disables all SDKs globally. These tests use an explicit in-memory
    # transport and keep pytest-socket active while exercising real serialization.
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)


async def observed(handler, telemetry):
    inner = HandlerTelemetryMiddleware(telemetry)
    outer = DispatchTelemetryMiddleware(telemetry)
    data = {"handler": SimpleNamespace(flags={"handler_key": "test.handler"}), "arbitrary": {"secret": CANARY}}

    async def selected(event, data):
        return await inner(handler, event, data)

    return await outer(selected, make_message(text=CANARY), data)


async def test_disabled_default_ignores_ambient_tokens_and_resources(monkeypatch):
    monkeypatch.setenv("LOGFIRE_TOKEN", CANARY)
    monkeypatch.setenv("LOGFIRE_API_KEY", CANARY)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", CANARY)
    sink = Capture()
    telemetry = Telemetry(transport=sink)
    await telemetry.start()
    with telemetry.operation(Boundary.HANDLER, "test.handler"):
        pass
    await telemetry.close()
    assert sink.started == 0 and sink.payloads == []
    assert trace.get_tracer_provider() is not telemetry._provider


async def test_every_signal_is_allowlisted_with_exception_baggage_and_environment_canaries(monkeypatch, caplog):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"host.name={CANARY},secret={CANARY}")
    monkeypatch.setenv("OTEL_SERVICE_NAME", CANARY)
    monkeypatch.setenv("LOGFIRE_TOKEN", CANARY)
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    sink = Capture()
    telemetry = Telemetry(config(release=CANARY), {"test.handler"}, transport=sink)
    await telemetry.start()
    token = context.attach(baggage.set_baggage("secret", CANARY))

    async def fail(event, data):
        with telemetry.operation(Boundary.PROVIDER, "jdoodle.execute", provider=Provider.JDOODLE) as operation:
            operation.http_status(503)
            error = RuntimeError(CANARY)
            error.add_note(CANARY)
            raise error from ValueError(CANARY)

    try:
        with pytest.raises(RuntimeError):
            await observed(fail, telemetry)
    finally:
        context.detach(token)
        await telemetry.close()
    serialized = sink.serialized()
    assert CANARY not in serialized and CANARY not in caplog.text
    spans = sink.spans()
    assert sorted(span.name for span in spans) == ["bot.handler", "provider.request"]
    assert sum(len(span.events) for span in spans) == 1
    assert next(span for span in spans if span.name == "bot.handler").events[0].name == "bot.failure"
    assert all(not span.status.message for span in spans)
    assert "exception.stacktrace" not in serialized and "exception.message" not in serialized
    for message in sink.messages():
        resources = message.resource_spans if isinstance(message, ExportTraceServiceRequest) else message.resource_metrics
        for resource in resources:
            assert {attribute.key for attribute in resource.resource.attributes} == {"service.name", "deployment.environment.name"}
            if isinstance(message, ExportMetricsServiceRequest):
                for scope in resource.scope_metrics:
                    assert scope.scope.name == "msu_hub_bot.telemetry"
                    assert {metric.name for metric in scope.metrics} <= {
                        "bot.operations",
                        "bot.operation.duration",
                        "bot.telemetry.dropped_spans",
                        "bot.poll.requests",
                        *(name.value for name in GaugeName),
                    }


async def test_dispatch_failure_before_handler_has_one_sanitized_owner():
    sink = Capture()
    telemetry = Telemetry(config(), transport=sink)
    await telemetry.start()
    with pytest.raises(RuntimeError):
        with telemetry.dispatch("message"):
            raise RuntimeError(CANARY)
    await telemetry.close()
    spans = sink.spans()
    assert len(spans) == 1 and spans[0].name == "bot.dispatch" and len(spans[0].events) == 1
    assert CANARY not in sink.serialized()


async def test_concurrent_contexts_and_detached_job_links_do_not_cross():
    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    await telemetry.start()
    ready, finish = asyncio.Event(), asyncio.Event()

    async def child():
        await finish.wait()
        with telemetry.operation(Boundary.JOB, "background"):
            with telemetry.operation(Boundary.PROVIDER, "http.request", provider=Provider.OTHER):
                pass

    async def first():
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            task = asyncio.create_task(child())
            ready.set()
            await asyncio.sleep(0)
        finish.set()
        await task

    async def second():
        await ready.wait()
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            with telemetry.operation(Boundary.PROVIDER, "jdoodle.execute", provider=Provider.JDOODLE):
                pass

    await asyncio.gather(first(), second())
    await telemetry.close()
    spans = sink.spans()
    handlers = [span for span in spans if span.name == "bot.handler"]
    assert len({span.trace_id for span in handlers}) == 2
    job = next(span for span in spans if span.name == "job.run")
    assert not job.parent_span_id and len(job.links) == 1
    assert job.trace_id not in {span.trace_id for span in handlers}
    assert job.links[0].trace_id in {span.trace_id for span in handlers}
    providers = [span for span in spans if span.name == "provider.request"]
    assert {span.parent_span_id for span in providers} <= {span.span_id for span in handlers + [job]}


async def test_cancellation_restores_context_without_exception_capture():
    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    await telemetry.start()
    started = asyncio.Event()

    async def work():
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with telemetry.operation(Boundary.HANDLER, "test.handler"):
        pass
    await telemetry.close()
    spans = sink.spans()
    assert len(spans) == 2 and len({span.trace_id for span in spans}) == 2
    assert not any(span.events for span in spans)
    assert {attribute.value.string_value for span in spans for attribute in span.attributes if attribute.key == "outcome"} == {
        "success",
        "cancelled",
    }


async def test_sampling_keeps_metrics_and_ignored_dispatch_does_not_create_spans():
    sink = Capture()
    telemetry = Telemetry(config(sample_rate=0), {"test.handler"}, transport=sink)
    await telemetry.start()
    with telemetry.dispatch("message") as observation:
        observation.outcome = Outcome.IGNORED
    with telemetry.operation(Boundary.HANDLER, "test.handler"):
        pass
    await telemetry.close()
    assert sink.spans() == []
    assert "bot.operations" in sink.serialized()
    assert "ignored" in sink.serialized() and "success" in sink.serialized()


async def test_queue_pressure_and_outage_do_not_block_handlers_or_leak_errors(caplog):
    class Unavailable(Capture):
        async def send(self, signal, payload):
            raise RuntimeError(CANARY)

    sink = Unavailable()
    telemetry = Telemetry(config(queue_capacity=2), {"test.handler"}, transport=sink)
    await telemetry.start()
    for _ in range(8):
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            pass
    assert len(telemetry._queue) == 2 and telemetry.dropped_spans == 6
    await telemetry.close()
    assert CANARY not in caplog.text
    assert caplog.text.count("Telemetry export unavailable") == 1
    assert sink.closed == 1


async def test_export_timeout_and_shutdown_cancel_the_async_transport():
    class Delayed(Capture):
        cancelled = 0

        async def send(self, signal, payload):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled += 1

    sink = Delayed()
    telemetry = Telemetry(config(request_timeout=0.02, shutdown_timeout=0.03), {"test.handler"}, transport=sink)
    await telemetry.start()
    with telemetry.operation(Boundary.HANDLER, "test.handler"):
        pass
    start = time.monotonic()
    await telemetry.close()
    assert time.monotonic() - start < 0.3
    assert sink.cancelled >= 1 and sink.closed == 1
    assert telemetry._worker.done()
    await telemetry.close()
    assert sink.closed == 1


def test_explicit_environment_loader_does_not_fall_back_to_management_token():
    config = TelemetryConfig.from_env({"LOGFIRE_API_KEY": CANARY, "HUB_TELEMETRY_ENABLED": "true"})
    assert config.token == "" and not config.valid()
    assert CANARY not in repr(config)


async def test_supervised_jobs_propagate_only_a_trace_link_and_report_failure_once():
    from contextvars import ContextVar
    from msu_hub_bot.telegram.runtime import Supervisor

    private_context = ContextVar("synthetic_private_context", default="empty")
    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    await telemetry.start()
    supervisor = Supervisor(telemetry)
    token = private_context.set(CANARY)

    async def job():
        assert private_context.get() == "empty"
        raise RuntimeError(CANARY)

    try:
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            task = supervisor.create_job(job)
        await asyncio.gather(task, return_exceptions=True)
        result = await supervisor.drain()
        assert result.failed_jobs == 1
    finally:
        private_context.reset(token)
        await telemetry.close()
    job_span = next(span for span in sink.spans() if span.name == "job.run")
    parent = next(span for span in sink.spans() if span.name == "bot.handler")
    assert job_span.links[0].trace_id == parent.trace_id
    assert not job_span.parent_span_id and len(job_span.events) == 1
    assert CANARY not in sink.serialized()


async def test_worker_cancellation_and_late_failure_have_separate_safe_measurements():
    import threading
    from contextvars import ContextVar
    from msu_hub_bot.execution.executor import TPExecutor

    private_context = ContextVar("synthetic_worker_context", default="empty")
    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    await telemetry.start()
    executor = TPExecutor(1, telemetry)
    started, finish = threading.Event(), threading.Event()

    def work():
        assert private_context.get() == "empty"
        started.set()
        assert finish.wait(2)
        raise RuntimeError(CANARY)

    async def first():
        token = private_context.set(CANARY)
        try:
            with telemetry.operation(Boundary.HANDLER, "test.handler"):
                assert await executor.run(work, timeout=0.05) == (None, True)
        finally:
            private_context.reset(token)

    try:
        await first()
        assert started.is_set()
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            finish.set()
            assert await executor.run(lambda: "done", timeout=1) == ("done", False)
        await asyncio.sleep(0)
    finally:
        finish.set()
        executor.shutdown(wait=True)
        await telemetry.close()
    spans = sink.spans()
    assert not any(span.events for span in spans)
    worker_outcomes = {
        attr.value.string_value for span in spans if span.name == "media.operation" for attr in span.attributes if attr.key == "outcome"
    }
    assert "timeout" in worker_outcomes and "success" in worker_outcomes
    serialized = sink.serialized()
    assert "worker.execute" in serialized and "unexpected" in serialized
    assert CANARY not in serialized


async def test_shared_provider_span_never_exports_request_or_response(monkeypatch):
    from msu_hub_bot.providers.jdoodle import JDoodle, JDoodleError

    class Response:
        status = 503

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def read(self):
            return CANARY.encode()

    sink = Capture()
    telemetry = Telemetry(config(), transport=sink)
    await telemetry.start()
    provider = JDoodle(CANARY, CANARY, telemetry)
    provider.__dict__["session"] = SimpleNamespace(post=lambda *args, **kwargs: Response())
    with pytest.raises(JDoodleError):
        await provider._request("execute", {"script": CANARY, "stdin": CANARY})
    await telemetry.close()
    span = sink.spans()[0]
    assert span.name == "provider.request" and not span.events
    assert CANARY not in sink.serialized()
    assert any(attr.key == "http.response.status_code" and attr.value.int_value == 503 for attr in span.attributes)


async def test_unknown_names_are_replaced_and_handler_registration_is_frozen():
    sink = Capture()
    telemetry = Telemetry(config(), transport=sink)
    telemetry.register_handlers({"test.handler"})
    await telemetry.start()
    with pytest.raises(RuntimeError, match="before start"):
        telemetry.register_handlers({CANARY})
    with telemetry.operation(Boundary.PROVIDER, CANARY, provider=CANARY):
        pass
    await asyncio.gather(telemetry.close(), telemetry.close())
    assert sink.closed == 1 and CANARY not in sink.serialized()


async def test_http_transport_has_explicit_endpoint_auth_and_no_ambient_credentials(monkeypatch):
    from msu_hub_bot import telemetry as module

    options = {}
    calls = []

    class Response:
        status = 302

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            options.update(kwargs)

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

        async def close(self):
            pass

    class Resolver:
        async def close(self):
            pass

    monkeypatch.setattr(module.aiohttp, "AsyncResolver", Resolver)
    monkeypatch.setattr(module.aiohttp, "TCPConnector", lambda **kwargs: kwargs)
    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    monkeypatch.setenv("HTTP_PROXY", CANARY)
    monkeypatch.setenv("LOGFIRE_API_KEY", CANARY)
    transport = module._HTTPTransport(config())
    await transport.start()
    assert not await transport.send("traces", b"protobuf")
    assert not await transport.send(CANARY, b"protobuf")
    await transport.close()
    assert options["trust_env"] is False and options["timeout"].total == 2
    assert isinstance(options["cookie_jar"], module.aiohttp.DummyCookieJar)
    assert len(calls) == 1 and calls[0][0] == "https://logfire-eu.pydantic.dev/v1/traces"
    assert calls[0][1]["allow_redirects"] is False and calls[0][1]["proxy"] is None
    assert calls[0][1]["headers"]["Authorization"] == "Bearer synthetic-project-write-token"
    assert CANARY not in str(calls)


async def test_real_aiohttp_dns_wait_is_bounded_and_cancelled_on_shutdown(monkeypatch):
    from msu_hub_bot import telemetry as module

    started, cancelled = asyncio.Event(), asyncio.Event()

    class Resolver:
        closed = False

        async def resolve(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def close(self):
            self.closed = True

    resolver = Resolver()
    monkeypatch.setattr(module.aiohttp, "AsyncResolver", lambda: resolver)
    telemetry = Telemetry(config(request_timeout=0.03, shutdown_timeout=0.08), {"test.handler"})
    await telemetry.start()
    try:
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            pass
        await asyncio.wait_for(started.wait(), 1)
    finally:
        await telemetry.close()
    assert cancelled.is_set() and resolver.closed
    assert telemetry._transport.session.closed and telemetry._worker.done()


async def test_skip_handler_is_an_ignored_outcome_not_a_failure():
    from aiogram.dispatcher.event.bases import SkipHandler

    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    await telemetry.start()

    async def skip(event, data):
        raise SkipHandler

    with pytest.raises(SkipHandler):
        await observed(skip, telemetry)
    await telemetry.close()
    assert len(sink.spans()) == 1 and not sink.spans()[0].events
    assert any(attr.key == "outcome" and attr.value.string_value == "ignored" for attr in sink.spans()[0].attributes)


async def test_successful_handler_then_storage_unwind_failure_has_one_linked_owner():
    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    await telemetry.start()
    with pytest.raises(RuntimeError):
        with telemetry.dispatch("message"):
            with telemetry.operation(Boundary.HANDLER, "test.handler"):
                pass
            raise RuntimeError(CANARY)
    await telemetry.close()
    spans = sink.spans()
    handler = next(span for span in spans if span.name == "bot.handler")
    dispatch = next(span for span in spans if span.name == "bot.dispatch")
    assert len(dispatch.events) == 1 and not handler.events
    assert dispatch.links[0].trace_id == handler.trace_id
    assert CANARY not in sink.serialized()


async def test_failed_startup_closes_allocated_resources_without_raw_error(caplog):
    class BrokenStartup(Capture):
        async def start(self):
            raise RuntimeError(CANARY)

    sink = BrokenStartup()
    telemetry = Telemetry(config(), transport=sink)
    await telemetry.start()
    with telemetry.operation(Boundary.HANDLER, "test.handler"):
        pass
    await telemetry.close()
    assert sink.closed == 1 and telemetry._closed
    assert not sink.payloads and CANARY not in caplog.text


async def test_passive_job_and_storage_emit_metrics_without_normal_traces():
    from msu_hub_bot.telegram.runtime import Supervisor

    sink = Capture()
    telemetry = Telemetry(config(), transport=sink)
    await telemetry.start()
    supervisor = Supervisor(telemetry)

    async def archive():
        with telemetry.operation(Boundary.STORAGE, "archive.write", trace=False):
            pass

    with telemetry.dispatch("message") as dispatch:
        with telemetry.operation(Boundary.STORAGE, "settings.load", trace=False):
            pass
        dispatch.outcome = Outcome.IGNORED
        await supervisor.create_job(archive, trace=False)
    await supervisor.drain()
    await telemetry.close()
    assert sink.spans() == []
    assert "archive.write" in sink.serialized() and "settings.load" in sink.serialized()


async def test_passive_job_failure_links_completed_handler_and_reports_once():
    from msu_hub_bot.telegram.runtime import Supervisor

    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    await telemetry.start()
    supervisor = Supervisor(telemetry)

    async def archive():
        with telemetry.operation(Boundary.STORAGE, "archive.write", trace=False):
            raise RuntimeError(CANARY)

    with telemetry.dispatch("message"):
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            pass
        await asyncio.gather(supervisor.create_job(archive, trace=False), return_exceptions=True)
    await supervisor.drain()
    await telemetry.close()
    spans = sink.spans()
    assert sorted(span.name for span in spans) == ["bot.handler", "job.run"]
    job = next(span for span in spans if span.name == "job.run")
    handler = next(span for span in spans if span.name == "bot.handler")
    assert len(job.events) == 1 and job.links[0].trace_id == handler.trace_id
    assert not job.parent_span_id and job.trace_id != handler.trace_id
    assert CANARY not in sink.serialized()


async def test_gauges_poll_health_and_unknown_sdk_instruments_are_allowlisted():
    sink = Capture()
    telemetry = Telemetry(config(), transport=sink)
    await telemetry.start()
    telemetry.gauge(GaugeName.JOBS_ACTIVE, 2)
    telemetry.gauge(GaugeName.JOBS_ACTIVE, float("nan"))
    telemetry.gauge(CANARY, 3)
    telemetry.record_poll(Outcome.SUCCESS)
    telemetry.record_poll(Outcome.UNAVAILABLE)
    telemetry._meter_provider.get_meter(CANARY).create_counter(CANARY).add(1, {"secret": CANARY})
    await telemetry.close()
    metrics = [
        metric
        for message in sink.messages()
        if isinstance(message, ExportMetricsServiceRequest)
        for resource in message.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    ]
    values = {metric.name: metric for metric in metrics}
    assert values["bot.jobs.active"].gauge.data_points[0].as_int == 2
    assert values["bot.poll.age"].gauge.data_points[0].as_double >= 0
    assert sum(point.as_int for point in values["bot.poll.requests"].sum.data_points) == 2
    assert CANARY not in sink.serialized()


async def test_root_trace_budget_keeps_children_and_metrics_consistent():
    sink = Capture()
    telemetry = Telemetry(config(traces_per_minute=1), {"test.handler"}, transport=sink)
    await telemetry.start()
    for _ in range(2):
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            with telemetry.operation(Boundary.PROVIDER, "http.request"):
                pass
    await telemetry.close()
    spans = sink.spans()
    assert sorted(span.name for span in spans) == ["bot.handler", "provider.request"]
    assert len({span.trace_id for span in spans}) == 1
    assert "bot.operations" in sink.serialized()


async def test_exporter_task_does_not_inherit_application_context():
    from contextvars import ContextVar

    private_context = ContextVar("synthetic_exporter_context", default="empty")

    class ContextCapture(Capture):
        async def send(self, signal, payload):
            assert private_context.get() == "empty"
            assert baggage.get_all() == {}
            return await super().send(signal, payload)

    sink = ContextCapture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    private_token = private_context.set(CANARY)
    baggage_token = context.attach(baggage.set_baggage("secret", CANARY))
    try:
        await telemetry.start()
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            pass
        await telemetry.close()
    finally:
        context.detach(baggage_token)
        private_context.reset(private_token)
    assert sink.spans() and CANARY not in sink.serialized()

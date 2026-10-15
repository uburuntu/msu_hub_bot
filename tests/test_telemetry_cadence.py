"""Metric batching preserves totals and does not postpone failure diagnostics."""

import asyncio

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.metrics.v1.metrics_pb2 import AGGREGATION_TEMPORALITY_DELTA

from msu_hub_bot.telemetry import Boundary, Outcome, Telemetry, TelemetryConfig
from telemetry_helpers import Capture, config

CANARY = "SYNTHETIC_PRIVATE_CADENCE_CANARY"


@pytest.fixture(autouse=True)
def local_sdk_capture(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)


def metric_batches(sink):
    return [
        {metric.name: metric for resource in message.resource_metrics for scope in resource.scope_metrics for metric in scope.metrics}
        for message in sink.messages()
        if isinstance(message, ExportMetricsServiceRequest)
    ]


def test_metrics_default_to_five_minutes():
    assert TelemetryConfig().interval == 300
    assert TelemetryConfig.from_env({}).interval == 300


@pytest.mark.parametrize("seconds", ["10", "60", "300"])
def test_explicit_metric_interval_is_validated(seconds):
    configured = TelemetryConfig.from_env(
        {"HUB_TELEMETRY_ENABLED": "true", "LOGFIRE_TOKEN": CANARY, "HUB_TELEMETRY_METRICS_INTERVAL_SECONDS": seconds}
    )
    assert configured.export and configured.valid()
    assert configured.interval == float(seconds)


@pytest.mark.parametrize("seconds", ["", "9.9", "300.1", "nan", "inf", "-inf", CANARY])
def test_invalid_metric_interval_disables_export_without_logging_value(seconds, caplog):
    configured = TelemetryConfig.from_env(
        {"HUB_TELEMETRY_ENABLED": "true", "LOGFIRE_TOKEN": CANARY, "HUB_TELEMETRY_METRICS_INTERVAL_SECONDS": seconds}
    )
    assert not configured.export
    assert CANARY not in caplog.text


async def test_unsampled_failure_is_exported_before_metric_deadline_and_shutdown_keeps_totals():
    class Notified(Capture):
        logs_sent = asyncio.Event()

        async def send(self, signal, payload):
            result = await super().send(signal, payload)
            if signal == "logs":
                self.logs_sent.set()
            return result

    sink = Notified()
    telemetry = Telemetry(config(sample_rate=0), {"test.handler"}, transport=sink)
    await telemetry.start()
    try:
        with telemetry.context(user_id=123456, chat_id=-654321):
            with telemetry.operation(Boundary.HANDLER, "test.handler"):
                pass
            with pytest.raises(RuntimeError), telemetry.operation(Boundary.HANDLER, "test.handler"):
                raise RuntimeError(CANARY)
        await asyncio.wait_for(sink.logs_sent.wait(), timeout=1)
        assert sink.spans() == []
        assert [log.body.string_value for log in sink.logs()] == ["bot.operation.failed"]
        assert metric_batches(sink) == []
    finally:
        await telemetry.close()

    metrics = metric_batches(sink)[0]
    assert "bot.operations" not in metrics
    durations = metrics["bot.operation.duration"].histogram
    assert durations.aggregation_temporality == AGGREGATION_TEMPORALITY_DELTA
    assert sum(point.count for point in durations.data_points) == 2
    assert {
        attribute.value.string_value for point in durations.data_points for attribute in point.attributes if attribute.key == "outcome"
    } == {
        "success",
        "unexpected",
    }
    assert all(not attribute.key.startswith("telegram.") for point in durations.data_points for attribute in point.attributes)
    assert CANARY not in sink.serialized()


async def test_periodic_metrics_do_not_require_logs_or_spans():
    class Notified(Capture):
        metrics_sent = asyncio.Event()

        async def send(self, signal, payload):
            result = await super().send(signal, payload)
            if signal == "metrics":
                self.metrics_sent.set()
            return result

    sink = Notified()
    telemetry = Telemetry(config(sample_rate=0, interval=0.01), {"test.handler"}, transport=sink)
    await telemetry.start()
    try:
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            pass
        await asyncio.wait_for(sink.metrics_sent.wait(), timeout=1)
        assert not sink.logs() and not sink.spans()
        assert metric_batches(sink)[0]["bot.operation.duration"].histogram.data_points[0].count == 1
    finally:
        await telemetry.close()


async def test_delta_export_sends_only_series_measured_since_the_previous_collection():
    sink = Capture()
    telemetry = Telemetry(config(sample_rate=0), {"test.handler"}, transport=sink)
    await telemetry.start()
    try:
        for _ in range(2):
            with telemetry.operation(Boundary.HANDLER, "test.handler"):
                pass
        telemetry.record_poll(Outcome.SUCCESS)
        await telemetry._metrics()
        await telemetry._metrics()
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            pass
    finally:
        await telemetry.close()

    batches = metric_batches(sink)
    assert [batch["bot.operation.duration"].histogram.data_points[0].count for batch in batches if "bot.operation.duration" in batch] == [
        2,
        1,
    ]
    assert ["bot.poll.requests" in batch for batch in batches] == [True, False, False]
    assert all(batch["bot.operation.duration"].histogram.aggregation_temporality == AGGREGATION_TEMPORALITY_DELTA for batch in batches[::2])
    assert batches[0]["bot.poll.requests"].sum.aggregation_temporality == AGGREGATION_TEMPORALITY_DELTA

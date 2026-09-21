"""Classifier diagnostics expose fixed decisions and usage through real OTLP payloads."""

import asyncio

import pytest
from opentelemetry import baggage, context
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

from msu_hub_bot.telemetry import Boundary, Provider, Telemetry
from telemetry_helpers import Capture, config

CANARY = "SYNTHETIC_PRIVATE_INTENT_PROMPT_987654"
COMMANDS = ("pdf", "text", "bg", "song", "anime", "none")


@pytest.fixture(autouse=True)
def local_sdk_capture(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)


def attributes(items):
    return {item.key: getattr(item.value, item.value.WhichOneof("value")) for item in items}


def metrics(capture):
    return {
        metric.name: metric
        for message in capture.messages()
        if isinstance(message, ExportMetricsServiceRequest)
        for resource in message.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


@pytest.mark.parametrize("sample_rate", [0, 1])
async def test_classification_logs_and_usage_survive_trace_sampling_without_metric_identity(sample_rate):
    capture = Capture()
    telemetry = Telemetry(config(sample_rate=sample_rate), transport=capture)
    await telemetry.start()
    token = context.attach(baggage.set_baggage("prompt", CANARY))
    try:
        with telemetry.context(user_id=42, chat_id=-10042, message_id=20, thread_id=3):
            for command in COMMANDS:
                with telemetry.operation(Boundary.PROVIDER, "jev.classify", provider=Provider.JEV) as operation:
                    operation.intent_result(command, 0.95, input_tokens=120, output_tokens=12, cost=0.00004)
                    operation.intent_result("none", 0.1, input_tokens=999, output_tokens=999, cost=999)
    finally:
        context.detach(token)
        await telemetry.close()

    logs = capture.logs()
    assert len(logs) == len(COMMANDS)
    assert all(record.body.string_value == "bot.intent.classified" for record in logs)
    results = [attributes(record.attributes) for record in logs]
    assert {result["intent.command"] for result in results} == set(COMMANDS)
    for result in results:
        assert result["intent.confidence"] == 0.95
        assert result["gen_ai.usage.input_tokens"] == 120
        assert result["gen_ai.usage.output_tokens"] == 12
        assert result["intent.cost_usd"] == 0.00004
        assert result["outcome"] == "success" and result["provider"] == "jev"
        assert result["telegram.user_id"] == 42 and result["telegram.chat_id"] == -10042
    assert len(capture.spans()) == len(COMMANDS) * sample_rate
    for span in capture.spans():
        result = attributes(span.attributes)
        assert result["intent.command"] in COMMANDS and result["intent.confidence"] == 0.95
        assert result["gen_ai.usage.input_tokens"] == 120
        assert not span.events

    all_metrics = metrics(capture)
    expected = {"bot.intent.input_tokens": 720, "bot.intent.output_tokens": 72, "bot.intent.cost": 0.00024}
    for name, total in expected.items():
        metric = all_metrics[name]
        assert metric.unit == ("USD" if name == "bot.intent.cost" else "{token}")
        assert sum(getattr(point, point.WhichOneof("value")) for point in metric.sum.data_points) == pytest.approx(total)
        assert all(attributes(point.attributes) == {"provider": "jev"} for point in metric.sum.data_points)
    for metric in all_metrics.values():
        for point in getattr(metric, metric.WhichOneof("data")).data_points:
            assert not any(key.startswith(("telegram.", "intent.", "command")) for key in attributes(point.attributes))
            assert not point.exemplars
    assert CANARY not in capture.serialized()


async def test_invalid_decisions_and_usage_never_become_attributes_or_spending():
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    await telemetry.start()
    try:
        with telemetry.operation(Boundary.PROVIDER, "jev.classify", provider=Provider.JEV) as operation:
            for invalid in (
                {"command": CANARY},
                {"command": None},
                {"confidence": CANARY},
                {"confidence": True},
                {"confidence": float("nan")},
                {"confidence": float("inf")},
                {"confidence": -0.1},
                {"confidence": 1.1},
                {"confidence": 10**400},
                {"input_tokens": CANARY},
                {"input_tokens": True},
                {"input_tokens": -1},
                {"input_tokens": 10**400},
                {"output_tokens": 1.5},
                {"output_tokens": float("inf")},
                {"cost": CANARY},
                {"cost": True},
                {"cost": -1},
                {"cost": float("nan")},
                {"cost": float("inf")},
                {"cost": 10**400},
            ):
                operation.intent_result(**({"command": "pdf", "confidence": 1} | invalid))
                assert not operation.details
            operation.intent_result("none", 0)
    finally:
        await telemetry.close()
    result = attributes(capture.logs()[0].attributes)
    assert result["intent.command"] == "none" and result["intent.confidence"] == 0
    assert not any(key.startswith("gen_ai.") or key == "intent.cost_usd" for key in result)
    assert not any(name.startswith("bot.intent.") for name in metrics(capture))
    assert CANARY not in capture.serialized()


async def test_results_belong_only_to_the_active_classifier_and_are_disabled_without_export():
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    await telemetry.start()
    try:
        for boundary, name, provider in (
            (Boundary.PROVIDER, "http.request", Provider.JEV),
            (Boundary.MEDIA, "jev.classify", Provider.JEV),
            (Boundary.PROVIDER, "jev.classify", Provider.OTHER),
        ):
            with telemetry.operation(boundary, name, provider=provider) as unrelated:
                unrelated.intent_result("pdf", 1, 999, 999, 999)
        with telemetry.operation(Boundary.PROVIDER, "jev.classify", provider=Provider.JEV) as operation:

            async def detached():
                operation.intent_result("pdf", 1, 999, 999, 999)

            await asyncio.create_task(detached())
            await asyncio.to_thread(operation.intent_result, "pdf", 1, 999, 999, 999)
            operation.intent_result("song", 1, 2, 3, 0)
        operation.intent_result("bg", 1, 999, 999, 999)
    finally:
        await telemetry.close()
    assert len(capture.logs()) == 1
    assert attributes(capture.logs()[0].attributes)["intent.command"] == "song"
    assert metrics(capture)["bot.intent.input_tokens"].sum.data_points[0].as_int == 2

    disabled = Capture()
    telemetry = Telemetry(transport=disabled)
    await telemetry.start()
    with telemetry.operation(Boundary.PROVIDER, "jev.classify", provider=Provider.JEV) as operation:
        operation.intent_result("pdf", 1, 1, 1, 1)
    await telemetry.close()
    assert not disabled.started and not disabled.payloads


@pytest.mark.parametrize("error,outcome", [(TimeoutError, "timeout"), (RuntimeError, "unexpected"), (asyncio.CancelledError, "cancelled")])
async def test_unsampled_failures_keep_fixed_logs_and_restore_mention_context(error, outcome):
    capture = Capture()
    telemetry = Telemetry(config(sample_rate=0), transport=capture)
    telemetry.register_commands(COMMANDS)
    await telemetry.start()
    try:
        with pytest.raises(error):
            with telemetry.context(command="text", command_kind="mention", user_id=42):
                with telemetry.operation(Boundary.PROVIDER, "jev.classify", provider=Provider.JEV) as operation:
                    operation.intent_result("text", 0.99, 9, 3, 0.001)
                    raise error(CANARY)
        with telemetry.operation(Boundary.PROVIDER, "jev.classify", provider=Provider.JEV):
            pass
    finally:
        await telemetry.close()
    assert not capture.spans() and len(capture.logs()) == 2
    failed, clean = [attributes(record.attributes) for record in capture.logs()]
    assert failed["outcome"] == outcome
    assert failed["command"] == "text" and failed["command.kind"] == "mention"
    assert failed["telegram.user_id"] == 42
    assert "command" not in clean and "command.kind" not in clean and "telegram.user_id" not in clean
    assert metrics(capture)["bot.intent.input_tokens"].sum.data_points[0].as_int == 9
    assert CANARY not in capture.serialized()


async def test_execution_records_separately_from_the_single_entry_handler_and_update():
    capture = Capture()
    telemetry = Telemetry(config(), handler_keys={"intent.entry", "process_topdf"}, transport=capture)
    telemetry.register_commands({"pdf"})
    await telemetry.start()
    try:
        with telemetry.dispatch("message"):
            with telemetry.operation(Boundary.HANDLER, "intent.entry"):
                with telemetry.context(command="pdf", command_kind="mention", handler="process_topdf"):
                    with telemetry.operation(Boundary.DISPATCH, "intent.execute"):
                        pass
    finally:
        await telemetry.close()
    counts = {
        (attributes(point.attributes)["boundary"], attributes(point.attributes)["operation"]): point.as_int
        for point in metrics(capture)["bot.operations"].sum.data_points
    }
    assert counts == {("bot.handler", "intent.entry"): 1, ("bot.dispatch", "intent.execute"): 1, ("bot.dispatch", "dispatch"): 1}
    execution = next(span for span in capture.spans() if attributes(span.attributes)["operation"] == "intent.execute")
    entry = next(span for span in capture.spans() if span.name == "bot.handler")
    assert execution.parent_span_id == entry.span_id
    assert attributes(execution.attributes)["handler"] == "process_topdf"
    assert attributes(execution.attributes)["command.kind"] == "mention"


@pytest.mark.parametrize("sample_rate", [0, 1])
async def test_language_resolution_usage_survives_sampling_without_extra_success_logs(sample_rate):
    capture = Capture()
    telemetry = Telemetry(config(sample_rate=sample_rate), transport=capture)
    await telemetry.start()
    try:
        with telemetry.context(user_id=42, chat_id=-10042):
            with telemetry.operation(Boundary.PROVIDER, "jev.resolve_languages", provider=Provider.JEV) as operation:
                for invalid in (True, -1, 10**400, CANARY):
                    operation.model_usage(input_tokens=invalid)
                    assert not operation.details
                operation.model_usage(100, 20, 0.0001)
                operation.model_usage(999, 999, 999)
            operation.model_usage(999, 999, 999)
    finally:
        await telemetry.close()
    assert not capture.logs()
    assert len(capture.spans()) == sample_rate
    for span in capture.spans():
        result = attributes(span.attributes)
        assert result["gen_ai.usage.input_tokens"] == 100
        assert result["model.cost_usd"] == 0.0001
        assert "intent.command" not in result
    expected = {"bot.model.input_tokens": 100, "bot.model.output_tokens": 20, "bot.model.cost": 0.0001}
    for name, total in expected.items():
        points = metrics(capture)[name].sum.data_points
        assert len(points) == 1
        assert getattr(points[0], points[0].WhichOneof("value")) == pytest.approx(total)
        assert attributes(points[0].attributes) == {"provider": "jev", "operation": "jev.resolve_languages"}
        assert not points[0].exemplars
    assert CANARY not in capture.serialized()


async def test_model_usage_is_owned_by_active_language_resolution_and_failure_is_safe():
    capture = Capture()
    telemetry = Telemetry(config(sample_rate=0), transport=capture)
    await telemetry.start()
    try:
        with telemetry.operation(Boundary.PROVIDER, "http.request", provider=Provider.JEV) as unrelated:
            unrelated.model_usage(999, 999, 999)
        with pytest.raises(TimeoutError):
            with telemetry.operation(Boundary.PROVIDER, "jev.resolve_languages", provider=Provider.JEV) as operation:

                async def detached():
                    operation.model_usage(999, 999, 999)

                await asyncio.create_task(detached())
                assert not operation.details
                raise TimeoutError(CANARY)
    finally:
        await telemetry.close()
    assert not any(name.startswith("bot.model.") for name in metrics(capture))
    (record,) = capture.logs()
    assert record.body.string_value == "bot.model.failed"
    assert attributes(record.attributes)["outcome"] == "timeout"
    assert CANARY not in capture.serialized()


@pytest.mark.parametrize("uncertain,expected", [(False, "rejected"), (True, "unavailable")])
async def test_command_delivery_failures_keep_safe_classification_without_raw_cause(uncertain, expected):
    from msu_hub_bot.telegram.responses import ResponseDeliveryError, ResponseProgress

    capture = Capture()
    telemetry = Telemetry(config(sample_rate=0), handler_keys={"test.delivery"}, transport=capture)
    await telemetry.start()
    error = ResponseDeliveryError(ResponseProgress(uncertain=uncertain, attempted_part=0, total_parts=1), RuntimeError(CANARY))
    try:
        with pytest.raises(ResponseDeliveryError):
            with telemetry.operation(Boundary.HANDLER, "test.delivery"):
                raise error
    finally:
        await telemetry.close()
    (record,) = capture.logs()
    fields = attributes(record.attributes)
    assert fields["outcome"] == expected
    assert fields["error.reason"] == ("delivery_uncertain" if uncertain else "delivery_rejected")
    assert CANARY not in capture.serialized()

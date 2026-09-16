"""Offline OTLP capture shared by SDK and real-dispatch privacy checks."""

import json
from dataclasses import replace

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from msu_hub_bot.telemetry import Environment, TelemetryConfig


class Capture:
    def __init__(self):
        self.payloads = []
        self.started = 0
        self.closed = 0

    async def start(self):
        self.started += 1

    async def send(self, signal, payload):
        self.payloads.append((signal, payload))
        return True

    async def close(self):
        self.closed += 1

    def messages(self):
        for signal, payload in self.payloads:
            message = (ExportTraceServiceRequest if signal == "traces" else ExportMetricsServiceRequest)()
            message.ParseFromString(payload)
            yield message

    def spans(self):
        return [
            span
            for message in self.messages()
            if isinstance(message, ExportTraceServiceRequest)
            for resource in message.resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]

    def serialized(self):
        return json.dumps([MessageToDict(message) for message in self.messages()])


def config(**changes):
    return replace(
        TelemetryConfig(export=True, token="synthetic-project-write-token", environment=Environment.TEST, sample_rate=1, interval=60),
        **changes,
    )

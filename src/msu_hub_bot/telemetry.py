"""Optional, allowlisted OTLP telemetry; no automatic instrumentation or credentials."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sys
import time
from collections import deque
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Context as ExecutionContext
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

import aiohttp
from opentelemetry._logs import SeverityNumber
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.metrics import CallbackOptions, NoOpMeterProvider, Observation
from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.view import DropAggregation, ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor, ReadableLogRecord, ReadWriteLogRecord
from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.sampling import Decision, ParentBased, Sampler, SamplingResult, TraceIdRatioBased
from opentelemetry.trace import INVALID_SPAN, Link, Span, SpanContext, SpanKind, StatusCode, TraceState, set_span_in_context
from opentelemetry.util.types import Attributes

logger = logging.getLogger(__name__)
EU_ENDPOINT = "https://logfire-eu.pydantic.dev"


class Boundary(StrEnum):
    HANDLER = "bot.handler"
    DISPATCH = "bot.dispatch"
    PROVIDER = "provider.request"
    MEDIA = "media.operation"
    STORAGE = "storage.operation"
    JOB = "job.run"
    TELEGRAM = "telegram.request"


class Outcome(StrEnum):
    SUCCESS = "success"
    IGNORED = "ignored"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNEXPECTED = "unexpected"


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Provider(StrEnum):
    TELEGRAM = "telegram"
    JDOODLE = "jdoodle"
    WIT = "wit"
    WOLFRAM = "wolfram"
    OTHER = "other"


class Backend(StrEnum):
    REDIS = "redis"
    SUPABASE = "supabase"
    NATIVE = "native"


class GaugeName(StrEnum):
    UPDATES_ACTIVE = "bot.updates.active"
    JOBS_ACTIVE = "bot.jobs.active"
    WORKERS_ACTIVE = "bot.workers.active"
    WORKERS_QUEUED = "bot.workers.queued"
    POLL_AGE = "bot.poll.age"


OPERATIONS = frozenset(
    {
        "dispatch",
        "http.request",
        "jdoodle.execute",
        "wit.recognize",
        "wolfram.query",
        "worker.queue",
        "worker.prepare",
        "worker.execute",
        "worker.run",
        "ffmpeg.convert",
        "settings.load",
        "settings.save",
        "archive.insert",
        "archive.write",
        "database.check",
        "database.read",
        "database.write",
        "database.auth",
        "feature.job",
        "deletion.enqueue",
        "deletion.due",
        "deletion.remove",
        "telegram.delete",
        "telegram.request",
        "background",
        "unknown",
    }
)
UPDATE_KINDS = frozenset(
    {
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "callback_query",
        "inline_query",
        "chosen_inline_result",
        "chat_member",
        "my_chat_member",
        "chat_join_request",
        "poll",
        "poll_answer",
        "message_reaction",
        "message_reaction_count",
        "unknown",
    }
)
METRIC_KEYS = {"boundary", "operation", "outcome", "provider", "backend", "update.kind"}


@dataclass(frozen=True)
class _RequestContext:
    attributes: tuple[tuple[str, str | int], ...] = ()


_request: ContextVar[_RequestContext | None] = ContextVar("telemetry_request", default=None)


def _request_attributes() -> dict[str, str | int]:
    context = _request.get()
    if context is None:
        return {}
    return dict(context.attributes)


def _identifier(value: object) -> bool:
    return type(value) is int and -(2**63) <= value < 2**63


def _telegram_methods() -> frozenset[str]:
    from aiogram import methods
    from aiogram.methods import TelegramMethod

    return frozenset(
        value.__api_method__
        for name in methods.__all__
        if isinstance(value := getattr(methods, name), type) and issubclass(value, TelegramMethod) and hasattr(value, "__api_method__")
    )


TELEGRAM_METHODS = _telegram_methods()


def _failure_location(error: BaseException) -> dict[str, str | int]:
    """Retain one real application code location without serializing a traceback."""
    root = Path(__file__).resolve().parent
    location: dict[str, str | int] = {}
    traceback = error.__traceback__
    for _ in range(64):
        if traceback is None:
            break
        frame, line = traceback.tb_frame, traceback.tb_lineno
        traceback = traceback.tb_next
        module_name = frame.f_globals.get("__name__")
        if not isinstance(module_name, str) or not module_name.startswith("msu_hub_bot."):
            continue
        module = sys.modules.get(module_name)
        filename = getattr(module, "__file__", None)
        function = frame.f_code.co_name
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", function):
            continue
        try:
            source = Path(frame.f_code.co_filename).resolve()
            relative = source.relative_to(root)
            if source != Path(filename).resolve() or not source.is_file():
                continue
        except OSError, RuntimeError, ValueError:
            continue
        location = {"code.file.path": f"msu_hub_bot/{relative.as_posix()}", "code.function.name": function, "code.line.number": line}
    return location


def safe_failure(error: BaseException) -> dict[str, str | int]:
    """Classify failures using vetted types and fixed descriptions, never their payloads."""
    from aiogram import exceptions as telegram
    from msu_hub_bot.providers.exceptions import ExternalServiceError
    from msu_hub_bot.settings import MissingIntegration
    from msu_hub_bot.execution.executor import ExecutorBusy
    from msu_hub_bot.media.limits import MediaDimensionsError
    from msu_hub_bot.telegram.files import DownloadTooLarge

    classes: tuple[tuple[type[BaseException], str, int | None], ...] = (
        (ExecutorBusy, "worker_busy", None),
        (DownloadTooLarge, "media_too_large", None),
        (MediaDimensionsError, "media_dimensions", None),
        (telegram.TelegramRetryAfter, "rate_limited", 429),
        (telegram.TelegramBadRequest, "bad_request", 400),
        (telegram.TelegramForbiddenError, "forbidden", 403),
        (telegram.TelegramUnauthorizedError, "unauthorized", 401),
        (telegram.TelegramEntityTooLarge, "entity_too_large", 413),
        (telegram.TelegramNotFound, "not_found", 404),
        (telegram.TelegramServerError, "server_error", 500),
        (telegram.TelegramNetworkError, "network_error", None),
        (telegram.TelegramAPIError, "telegram_error", None),
        (asyncio.CancelledError, "cancelled", None),
        (TimeoutError, "timeout", None),
        (ExternalServiceError, "provider_unavailable", None),
        (MissingIntegration, "configuration_missing", None),
        (aiohttp.ClientError, "network_error", None),
        (OSError, "io_error", None),
        (ValueError, "invalid_value", None),
        (TypeError, "invalid_type", None),
        (RuntimeError, "unexpected", None),
    )
    attributes: dict[str, str | int] = {"error.type": "Exception", "error.reason": "unexpected"}
    for kind, reason, status in classes:
        if isinstance(error, kind):
            attributes.update({"error.type": kind.__name__, "error.reason": reason})
            if status is not None:
                attributes["http.response.status_code"] = status
            break
    if isinstance(error, telegram.TelegramAPIError):
        message = error.message.casefold().removeprefix("bad request: ").removeprefix("forbidden: ")
        reasons = {
            "chat not found": "chat_not_found",
            "private chat not found": "chat_not_found",
            "the group chat was deleted": "chat_deleted",
            "message to edit not found": "message_not_found",
            "message to delete not found": "message_not_found",
            "message can't be edited": "message_not_editable",
            "message can't be deleted": "message_not_deletable",
            "bot was blocked by the user": "bot_blocked",
            "bot was kicked from the supergroup chat": "bot_removed",
            "bot was kicked from the group chat": "bot_removed",
            "bot was kicked from the channel chat": "bot_removed",
            "bot is not a member of the group chat": "bot_removed",
            "bot is not a member of the supergroup chat": "bot_removed",
            "bot is not a member of the channel chat": "bot_removed",
            "user is deactivated": "user_deactivated",
            "have no rights to send a message": "not_enough_rights",
            "not enough rights to send text messages to the chat": "not_enough_rights",
            "not enough rights to send photos to the chat": "not_enough_rights",
            "query is too old and response timeout expired or query id is invalid": "query_expired",
            "message caption is too long": "caption_too_long",
            "message is too long": "text_too_long",
        }
        if message in reasons:
            attributes["error.reason"] = reasons[message]
        elif message.startswith("message is not modified"):
            attributes["error.reason"] = "message_not_modified"
        elif message.startswith("can't parse entities"):
            attributes["error.reason"] = "invalid_entities"
    if isinstance(error, telegram.TelegramRetryAfter) and type(error.retry_after) is int:
        attributes["telegram.retry_after"] = min(86400, max(0, error.retry_after))
    if isinstance(error, aiohttp.ClientResponseError) and type(error.status) is int and 100 <= error.status <= 599:
        attributes["http.response.status_code"] = error.status
    attributes.update(_failure_location(error))
    return attributes


@dataclass(frozen=True)
class TelemetryConfig:
    export: bool = False
    token: str = field(default="", repr=False)
    environment: Environment = Environment.PRODUCTION
    release: str = ""
    sample_rate: float = 0.1
    traces_per_minute: int = 60
    queue_capacity: int = 256
    batch_size: int = 32
    interval: float = 5
    request_timeout: float = 2
    shutdown_timeout: float = 3

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> TelemetryConfig:
        """The composition root explicitly selects the environment to read."""
        try:
            return cls(
                export=env.get("HUB_TELEMETRY_ENABLED", "").casefold() in {"1", "true", "yes"},
                token=env.get("LOGFIRE_TOKEN", ""),
                environment=Environment(env.get("HUB_ENVIRONMENT", "production")),
                release=env.get("HUB_RELEASE", ""),
                sample_rate=float(env.get("HUB_TELEMETRY_SAMPLE_RATE", "0.1")),
            )
        except ValueError:
            logger.warning("Telemetry configuration is invalid; export remains disabled")
            return cls()

    def valid(self) -> bool:
        return (
            isinstance(self.environment, Environment)
            and 8 <= len(self.token) <= 2048
            and all(32 < ord(c) < 127 for c in self.token)
            and math.isfinite(self.sample_rate)
            and 0 <= self.sample_rate <= 1
            and 1 <= self.traces_per_minute <= 600
            and 1 <= self.queue_capacity <= 1024
            and 1 <= self.batch_size <= 64
            and 0.01 <= self.interval <= 60
            and 0.01 <= self.request_timeout <= 5
            and 0.01 <= self.shutdown_timeout <= 10
        )


class ExportTransport(Protocol):
    async def start(self) -> None: ...
    async def send(self, signal: str, payload: bytes) -> bool: ...
    async def close(self) -> None: ...


class _HTTPTransport:
    def __init__(self, config: TelemetryConfig) -> None:
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.resolver: aiohttp.AsyncResolver | None = None

    async def start(self) -> None:
        self.resolver = aiohttp.AsyncResolver()
        connector = aiohttp.TCPConnector(resolver=self.resolver, limit=1)
        self.session = aiohttp.ClientSession(
            connector=connector,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            headers={"User-Agent": "msu-hub-bot-telemetry"},
            timeout=aiohttp.ClientTimeout(total=self.config.request_timeout, connect=self.config.request_timeout),
        )

    async def send(self, signal: str, payload: bytes) -> bool:
        if self.session is None or signal not in {"traces", "metrics", "logs"}:
            return False
        async with self.session.post(
            f"{EU_ENDPOINT}/v1/{signal}",
            data=payload,
            headers={"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/x-protobuf"},
            allow_redirects=False,
            proxy=None,
        ) as response:
            # Response bodies and request/exception representations are never diagnostics.
            return 200 <= response.status < 300

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
        if self.resolver is not None:
            await self.resolver.close()


class _RootSampler(Sampler):
    def __init__(self, rate: float, capacity: int) -> None:
        self.ratio = TraceIdRatioBased(rate)
        self.capacity = capacity
        self.remaining = capacity
        self.reset_at = time.monotonic() + 60

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if time.monotonic() >= self.reset_at:
            self.remaining, self.reset_at = self.capacity, time.monotonic() + 60
        result = self.ratio.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)
        if result.decision is Decision.RECORD_AND_SAMPLE and self.remaining:
            self.remaining -= 1
            return result
        return SamplingResult(Decision.DROP)

    def get_description(self) -> str:
        return "Bounded application root sampling"


@dataclass
class _Dispatch:
    owner: asyncio.Task[Any] | None
    kind: str
    reported: bool = False
    outcome: Outcome = Outcome.SUCCESS
    last_span: SpanContext | None = None
    last_context: tuple[tuple[str, str | int], ...] = ()


_dispatch: ContextVar[_Dispatch | None] = ContextVar("telemetry_dispatch", default=None)
_current: ContextVar[Operation | None] = ContextVar("telemetry_operation", default=None)
_job_link: ContextVar[SpanContext | None] = ContextVar("telemetry_job_link", default=None)


def failure_outcome(error: BaseException) -> Outcome:
    # Classification examines types only: Telegram/provider exception strings contain inputs.
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
    from aiogram.dispatcher.event.bases import CancelHandler, SkipHandler
    from msu_hub_bot.providers.exceptions import ExternalServiceError
    from msu_hub_bot.execution.executor import ExecutorBusy
    from msu_hub_bot.media.limits import MediaDimensionsError
    from msu_hub_bot.telegram.files import DownloadTooLarge

    if isinstance(error, SkipHandler):
        return Outcome.IGNORED
    if isinstance(error, (CancelHandler, ExecutorBusy, DownloadTooLarge, MediaDimensionsError)):
        return Outcome.REJECTED
    if isinstance(error, asyncio.CancelledError):
        return Outcome.CANCELLED
    if isinstance(error, TimeoutError):
        return Outcome.TIMEOUT
    if isinstance(error, (TelegramBadRequest, TelegramForbiddenError)) or (type(error).__module__, type(error).__name__) == (
        "msu_hub_bot.settings",
        "MissingIntegration",
    ):
        return Outcome.REJECTED
    if isinstance(error, (TelegramNetworkError, TelegramRetryAfter, ExternalServiceError, OSError)):
        return Outcome.UNAVAILABLE
    return Outcome.UNEXPECTED


class Operation:
    def __init__(self, owner: asyncio.Task[Any] | None, span: Span | None = None) -> None:
        self.owner, self._span = owner, span
        self.outcome = Outcome.SUCCESS
        self.active = True
        self.failure: dict[str, str | int] = {}
        self.details: dict[str, int] = {}

    def set_outcome(self, outcome: Outcome) -> None:
        if isinstance(outcome, Outcome):
            self.outcome = outcome

    def http_status(self, status: int) -> None:
        if type(status) is int and 100 <= status <= 599:
            self.details["http.response.status_code"] = status
            if self._span is not None:
                self._span.set_attribute("http.response.status_code", status)

    def request_attempt(self, attempt: int) -> None:
        if type(attempt) is int:
            self.details["attempt"] = min(10, max(1, attempt))
            if self._span is not None:
                self._span.set_attribute("attempt", self.details["attempt"])


class _QueueProcessor(SpanProcessor):
    def __init__(self, telemetry: Telemetry) -> None:
        self.telemetry = telemetry

    def on_end(self, span: ReadableSpan) -> None:
        self.telemetry._enqueue(span)


class _LogQueueProcessor(LogRecordProcessor):
    def __init__(self, telemetry: Telemetry) -> None:
        self.telemetry = telemetry

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        if log_record.resource is not None:
            self.telemetry._enqueue_log(
                ReadableLogRecord(log_record.log_record, log_record.resource, log_record.instrumentation_scope, log_record.limits)
            )

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class Telemetry:
    def __init__(
        self,
        config: TelemetryConfig | None = None,
        handler_keys: Collection[str] = (),
        *,
        transport: ExportTransport | None = None,
    ) -> None:
        self.config = config or TelemetryConfig()
        self.handler_keys = frozenset(key for key in handler_keys if re.fullmatch(r"[A-Za-z_][\w.]{0,159}", key))
        self.command_keys: frozenset[str] = frozenset()
        self._transport = transport
        self._queue: deque[ReadableSpan] = deque()
        self._log_queue: deque[ReadableLogRecord] = deque()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._provider: TracerProvider | None = None
        self._logger_provider: LoggerProvider | None = None
        self._meter_provider: MeterProvider | None = None
        self._reader: InMemoryMetricReader | None = None
        self._gauge_values: dict[GaugeName, float] = {}
        self._last_poll: float | None = None
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._outage = False
        self._log_emit_failed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self.dropped_spans = 0
        self.dropped_logs = 0

    def register_handlers(self, keys: Collection[str]) -> None:
        """Freeze registration-derived names before export starts."""
        if self._worker is not None or self._closed:
            raise RuntimeError("Register telemetry handlers before start")
        self.handler_keys = frozenset(key for key in keys if re.fullmatch(r"[A-Za-z_][\w.]{0,159}", key))

    def register_commands(self, keys: Collection[str]) -> None:
        """Only literal registered command aliases are useful diagnostic dimensions."""
        if self._worker is not None or self._closed:
            raise RuntimeError("Register telemetry commands before start")
        self.command_keys = frozenset(key.casefold() for key in keys if re.fullmatch(r"[\w-]{1,64}", key))

    @contextmanager
    def context(
        self,
        *,
        user_id: int | None = None,
        actor_chat_id: int | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        thread_id: int | None = None,
        update_id: int | None = None,
        reply_to_message_id: int | None = None,
        handler: str | None = None,
        command: str | None = None,
        command_kind: str | None = None,
    ) -> Iterator[None]:
        attributes = _request_attributes()
        for key, identifier in (
            ("user_id", user_id),
            ("actor_chat_id", actor_chat_id),
            ("chat_id", chat_id),
            ("message_id", message_id),
            ("thread_id", thread_id),
            ("update_id", update_id),
            ("reply_to_message_id", reply_to_message_id),
        ):
            if _identifier(identifier):
                attributes[f"telegram.{key}"] = cast(int, identifier)
        if handler in self.handler_keys:
            attributes["handler"] = handler
        if isinstance(command, str) and command.casefold() in self.command_keys:
            attributes["command"] = command.casefold()
            if command_kind in {"slash", "hashtag"}:
                attributes["command.kind"] = command_kind
        token = _request.set(_RequestContext(tuple(attributes.items())))
        try:
            yield
        finally:
            _request.reset(token)

    def job_context(self) -> ExecutionContext:
        """Carry only explicit request identifiers and a trace link into a job."""
        clean = ExecutionContext()
        attributes = _request_attributes()
        dispatch = _dispatch.get()
        if dispatch is not None and dispatch.owner is asyncio.current_task():
            attributes = {**dict(dispatch.last_context), **attributes}
        if attributes:
            clean.run(_request.set, _RequestContext(tuple(attributes.items())))
        current = _current.get()
        if current is not None and current._span is not None and current.owner is asyncio.current_task() and current.active:
            clean.run(_job_link.set, current._span.get_span_context())
        elif dispatch is not None and dispatch.owner is asyncio.current_task() and dispatch.last_span is not None:
            clean.run(_job_link.set, dispatch.last_span)
        return clean

    def gauge(self, name: GaugeName, value: float) -> None:
        if self._available() and isinstance(name, GaugeName) and math.isfinite(value) and 0 <= value <= 1e9:
            self._gauge_values[name] = value

    def _gauge_callback(self, name: GaugeName) -> Callable[[CallbackOptions], Sequence[Observation]]:
        def observe(options: CallbackOptions) -> Sequence[Observation]:
            if name is GaugeName.POLL_AGE and self._last_poll is not None:
                return [Observation(max(0, time.monotonic() - self._last_poll))]
            value = self._gauge_values.get(name)
            return [] if value is None else [Observation(value)]

        return observe

    def record_poll(self, outcome: Outcome) -> None:
        if self._available() and isinstance(outcome, Outcome):
            self._poll_count.add(1, {"outcome": outcome.value})
            if outcome is Outcome.SUCCESS:
                self._last_poll = time.monotonic()

    def record_media_completion(self, duration: float, outcome: Outcome) -> None:
        """A late thread completion contributes metrics, never another task's trace."""
        self._measure(Boundary.MEDIA, "worker.execute", outcome, duration, backend=Backend.NATIVE.value)

    async def start(self) -> None:
        if self._worker is not None or self._closed or not self.config.export:
            return
        try:
            valid = self.config.valid()
        except TypeError, ValueError, AttributeError:
            valid = False
        if not valid:
            logger.warning("Telemetry configuration is invalid; export remains disabled")
            return
        try:
            self._loop = asyncio.get_running_loop()
            resource_values = {"service.name": "msu-hub-bot", "deployment.environment.name": self.config.environment.value}
            if re.fullmatch(r"(?:[a-f0-9]{7,40}|v?\d+\.\d+\.\d+)", self.config.release):
                resource_values["service.version"] = self.config.release
            resource = Resource(resource_values)
            self._reader = InMemoryMetricReader()
            self._meter_provider = MeterProvider(
                metric_readers=[self._reader],
                resource=resource,
                shutdown_on_exit=False,
                exemplar_filter=AlwaysOffExemplarFilter(),
                views=[
                    View(instrument_name="*", aggregation=DropAggregation()),
                    View(instrument_name="bot.operations", meter_name="msu_hub_bot.telemetry", attribute_keys=METRIC_KEYS),
                    View(
                        instrument_name="bot.operation.duration",
                        meter_name="msu_hub_bot.telemetry",
                        attribute_keys=METRIC_KEYS,
                        aggregation=ExplicitBucketHistogramAggregation([0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 30, 180]),
                    ),
                    View(instrument_name="bot.telemetry.dropped_spans", meter_name="msu_hub_bot.telemetry", attribute_keys=set()),
                    View(instrument_name="bot.telemetry.dropped_logs", meter_name="msu_hub_bot.telemetry", attribute_keys=set()),
                    View(instrument_name="bot.poll.requests", meter_name="msu_hub_bot.telemetry", attribute_keys={"outcome"}),
                    *[View(instrument_name=name.value, meter_name="msu_hub_bot.telemetry", attribute_keys=set()) for name in GaugeName],
                ],
            )
            meter = self._meter_provider.get_meter("msu_hub_bot.telemetry", version="1")
            self._count = meter.create_counter("bot.operations", unit="1")
            self._duration = meter.create_histogram("bot.operation.duration", unit="s")
            self._drops = meter.create_counter("bot.telemetry.dropped_spans", unit="1")
            self._log_drops = meter.create_counter("bot.telemetry.dropped_logs", unit="1")
            self._poll_count = meter.create_counter("bot.poll.requests", unit="1")
            for name in GaugeName:
                meter.create_observable_gauge(
                    name.value, callbacks=[self._gauge_callback(name)], unit="s" if name is GaugeName.POLL_AGE else "1"
                )
            self._provider = TracerProvider(
                resource=resource,
                sampler=ParentBased(_RootSampler(self.config.sample_rate, self.config.traces_per_minute)),
                shutdown_on_exit=False,
                meter_provider=NoOpMeterProvider(),
                span_limits=SpanLimits(
                    max_attributes=32,
                    max_events=1,
                    max_links=1,
                    max_span_attributes=32,
                    max_event_attributes=8,
                    max_link_attributes=0,
                    max_attribute_length=160,
                    max_span_attribute_length=160,
                ),
            )
            self._provider.add_span_processor(_QueueProcessor(self))
            self._tracer = self._provider.get_tracer("msu_hub_bot.telemetry", "1")
            self._logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False, meter_provider=NoOpMeterProvider())
            self._logger_provider.add_log_record_processor(_LogQueueProcessor(self))
            self._structured_logger = self._logger_provider.get_logger("msu_hub_bot.telemetry", "1")
            self._transport = self._transport or _HTTPTransport(self.config)
            await self._transport.start()
            self._worker = asyncio.create_task(self._export_loop(), name="bot-telemetry-export", context=ExecutionContext())
        except Exception:
            logger.warning("Telemetry could not start; export remains disabled")
            await self.close()

    def _available(self) -> bool:
        try:
            return self._worker is not None and not self._worker.done() and not self._closing and asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _enqueue(self, span: ReadableSpan) -> None:
        if not self._available():
            return
        if len(self._queue) >= self.config.queue_capacity:
            self.dropped_spans += 1
            self._drops.add(1)
            return
        self._queue.append(span)
        self._wake.set()

    def _enqueue_log(self, record: ReadableLogRecord) -> None:
        if not self._available():
            return
        if len(self._log_queue) >= self.config.queue_capacity:
            self.dropped_logs += 1
            self._log_drops.add(1)
            return
        self._log_queue.append(record)
        self._wake.set()

    def _operation_log(self, boundary: Boundary, handle: Operation, attributes: dict[str, str | int], duration: float) -> None:
        failed = handle.outcome not in {Outcome.SUCCESS, Outcome.IGNORED, Outcome.CANCELLED}
        span = handle._span
        if boundary is Boundary.TELEGRAM:
            if not failed or handle.failure.get("error.reason") == "message_not_modified":
                return
            event = "telegram.request.failed"
        elif boundary in {Boundary.HANDLER, Boundary.JOB, Boundary.DISPATCH}:
            if not failed and (span is None or not span.is_recording()):
                return
            event = "bot.operation.failed" if failed else "bot.operation.completed"
        else:
            return
        # An explicit empty context prevents ambient baggage or unrelated traces.
        context = set_span_in_context(span if span is not None else INVALID_SPAN, Context())
        severity = SeverityNumber.ERROR if handle.outcome is Outcome.UNEXPECTED else SeverityNumber.WARN if failed else SeverityNumber.INFO
        try:
            self._structured_logger.emit(
                timestamp=time.time_ns(),
                body=event,
                event_name=event,
                context=context,
                severity_number=severity,
                severity_text=severity.name,
                attributes={
                    **attributes,
                    **handle.details,
                    **handle.failure,
                    "boundary": boundary.value,
                    "outcome": handle.outcome.value,
                    "duration_ms": round(max(0, duration) * 1000, 3),
                },
            )
        except Exception:
            self.dropped_logs += 1
            try:
                self._log_drops.add(1)
            except Exception:
                pass
            if not self._log_emit_failed:
                logger.warning("Telemetry log emission failed; records will be dropped")
                self._log_emit_failed = True
        else:
            self._log_emit_failed = False

    def _measure(self, boundary: Boundary, operation: str, outcome: Outcome, duration: float, **labels: str) -> None:
        if self._meter_provider is not None and not self._closed:
            attributes = {"boundary": boundary.value, "operation": operation, "outcome": outcome.value, **labels}
            self._count.add(1, attributes)
            self._duration.record(max(0, duration), attributes)

    @contextmanager
    def operation(
        self,
        boundary: Boundary,
        operation: str,
        *,
        provider: Provider | None = None,
        backend: Backend | None = None,
        attempt: int = 1,
        trace: bool = True,
        telegram_method: str | None = None,
        target_chat_id: int | None = None,
        target_message_id: int | None = None,
    ) -> Iterator[Operation]:
        if not self._available():
            yield Operation(None)
            return
        owner = asyncio.current_task()
        valid_keys = self.handler_keys if boundary is Boundary.HANDLER else OPERATIONS
        key = operation if operation in valid_keys else "unknown"
        attributes = _request_attributes()
        if boundary is Boundary.HANDLER:
            attributes["handler"] = key
        dispatch = _dispatch.get()
        dispatch = dispatch if dispatch is not None and dispatch.owner is owner else None
        if dispatch is not None and boundary is not Boundary.HANDLER:
            attributes = {**dict(dispatch.last_context), **attributes}
        request_token = _request.set(_RequestContext(tuple(attributes.items())))
        labels: dict[str, str] = {}
        if isinstance(provider, Provider):
            labels["provider"] = provider.value
        if isinstance(backend, Backend):
            labels["backend"] = backend.value
        if dispatch is not None:
            labels["update.kind"] = dispatch.kind
        attributes.update({"operation": key, **labels})
        if boundary is Boundary.TELEGRAM:
            attributes["telegram.method"] = telegram_method if telegram_method in TELEGRAM_METHODS else "unknown"
            for name, identifier in (("target_chat_id", target_chat_id), ("target_message_id", target_message_id)):
                if _identifier(identifier):
                    attributes[f"telegram.{name}"] = cast(int, identifier)
        if boundary is Boundary.PROVIDER and isinstance(attempt, int):
            attributes["attempt"] = min(10, max(1, attempt))
        parent = _current.get()
        context = Context()
        links = []
        if boundary is Boundary.JOB and (job_link := _job_link.get()) is not None:
            links = [Link(job_link)]
        if parent is not None and parent._span is not None:
            if boundary is Boundary.JOB:
                links = [Link(parent._span.get_span_context())]
            elif parent.active:
                context = set_span_in_context(parent._span, Context())
        if boundary is Boundary.DISPATCH and dispatch is not None and dispatch.last_span is not None:
            links = [Link(dispatch.last_span)]

        def start_span() -> Span:
            return self._tracer.start_span(
                boundary.value, context=context, attributes=attributes, links=links, record_exception=False, set_status_on_exception=False
            )

        span = start_span() if trace else None
        handle = Operation(owner, span)
        token = _current.set(handle)
        start = time.monotonic()
        try:
            yield handle
        except BaseException as error:
            handle.failure = safe_failure(error)
            if span is not None:
                span.set_attributes(handle.failure)
            if handle.outcome is Outcome.SUCCESS:
                handle.set_outcome(failure_outcome(error))
            if boundary in {Boundary.HANDLER, Boundary.JOB, Boundary.DISPATCH} and handle.outcome not in {
                Outcome.CANCELLED,
                Outcome.REJECTED,
                Outcome.IGNORED,
            }:
                if dispatch is None or not dispatch.reported or boundary is Boundary.JOB:
                    # Passive archive/prefilter work emits metrics; only its owning
                    # failure boundary may create a sampled incident trace.
                    if span is None:
                        span = start_span()
                        handle._span = span
                        span.set_attributes(handle.failure)
                    span.add_event("bot.failure", {"failure.category": handle.outcome.value, **handle.failure})
                    if dispatch is not None and boundary is not Boundary.JOB:
                        dispatch.reported = True
            raise
        finally:
            try:
                duration = time.monotonic() - start
                if span is not None:
                    span.set_attribute("outcome", handle.outcome.value)
                    if handle.outcome is Outcome.UNEXPECTED:
                        span.set_status(StatusCode.ERROR)
                if boundary is not Boundary.DISPATCH:
                    self._measure(boundary, key, handle.outcome, duration, **labels)
                self._operation_log(boundary, handle, attributes, duration)
                if dispatch is not None and boundary is Boundary.HANDLER and span is not None:
                    dispatch.last_span = span.get_span_context()
                    dispatch.last_context = tuple(_request_attributes().items())
            except Exception:
                logger.warning("Telemetry operation diagnostics failed")
            finally:
                handle.active = False
                _current.reset(token)
                _request.reset(request_token)
                if span is not None:
                    try:
                        span.end()
                    except Exception:
                        logger.warning("Telemetry span completion failed")

    @contextmanager
    def dispatch(self, kind: str) -> Iterator[_Dispatch]:
        state = _Dispatch(asyncio.current_task(), kind if kind in UPDATE_KINDS else "unknown")
        token = _dispatch.set(state)
        attributes = {**_request_attributes(), "update.kind": state.kind}
        request_token = _request.set(_RequestContext(tuple(attributes.items())))
        start = time.monotonic()
        try:
            yield state
        except BaseException as error:
            state.outcome = failure_outcome(error)
            if not state.reported and self._available() and state.outcome not in {Outcome.CANCELLED, Outcome.REJECTED, Outcome.IGNORED}:
                # Raise only inside our manual boundary; the SDK never receives the exception.
                with self.operation(Boundary.DISPATCH, "dispatch"):
                    raise
            raise
        finally:
            self._measure(Boundary.DISPATCH, "dispatch", state.outcome, time.monotonic() - start, **{"update.kind": state.kind})
            _dispatch.reset(token)
            _request.reset(request_token)

    async def _send(self, signal: str, payload: bytes) -> None:
        if self._transport is None:
            return
        try:
            async with asyncio.timeout(self.config.request_timeout):
                success = await self._transport.send(signal, payload)
        except Exception:
            success = False
        if not success and not self._outage:
            logger.warning("Telemetry export unavailable; batches will be dropped")
        elif success and self._outage:
            logger.info("Telemetry export recovered")
        self._outage = not success

    async def _metrics(self) -> None:
        if self._reader is not None:
            data = self._reader.get_metrics_data()
            if data is not None:
                await self._send("metrics", cast(bytes, encode_metrics(data).SerializeToString()))

    async def _export_loop(self) -> None:
        next_metrics = time.monotonic() + self.config.interval
        while True:
            if self._queue:
                batch = [self._queue.popleft() for _ in range(min(self.config.batch_size, len(self._queue)))]
                await self._send("traces", cast(bytes, encode_spans(batch).SerializeToString()))
            if self._log_queue:
                records = [self._log_queue.popleft() for _ in range(min(self.config.batch_size, len(self._log_queue)))]
                await self._send("logs", cast(bytes, encode_logs(records).SerializeToString()))
            if time.monotonic() >= next_metrics:
                await self._metrics()
                next_metrics = time.monotonic() + self.config.interval
            if self._closing and not self._queue and not self._log_queue:
                await self._metrics()
                return
            if not self._queue and not self._log_queue:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), max(0.001, next_metrics - time.monotonic()))
                except TimeoutError:
                    pass

    async def close(self) -> None:
        async with self._close_lock:
            await self._close_once()

    async def _close_once(self) -> None:
        if self._closed:
            return
        deadline = time.monotonic() + self.config.shutdown_timeout
        cleanup_reserve = min(self.config.request_timeout, self.config.shutdown_timeout / 3)
        self._closing = True
        self._wake.set()
        try:
            if self._worker is not None:
                try:
                    await asyncio.wait_for(self._worker, timeout=self.config.shutdown_timeout - cleanup_reserve)
                except TimeoutError:
                    logger.warning("Telemetry flush exceeded its deadline; remaining batches were dropped")
                except Exception:
                    logger.warning("Telemetry export stopped; remaining batches were dropped")
        finally:
            self._closed = True
            self._queue.clear()
            self._log_queue.clear()
            if self._transport is not None:
                try:
                    async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                        await self._transport.close()
                except Exception:
                    logger.warning("Telemetry transport cleanup failed")
            if self._provider is not None:
                self._provider.shutdown()
            if self._logger_provider is not None:
                self._logger_provider.shutdown()
            if self._meter_provider is not None:
                self._meter_provider.shutdown()

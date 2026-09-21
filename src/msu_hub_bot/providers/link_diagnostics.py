"""Bounded, content-free outcomes collected across one link extraction."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum


class LinkStage(StrEnum):
    ROUTE = "route"
    REQUEST = "request"
    EXTRACT = "extract"
    IMAGE = "image"
    VIDEO = "video"
    ADAPTER = "adapter"
    DELIVERY = "delivery"


class LinkReason(StrEnum):
    OK = "ok"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    TOO_LARGE = "too_large"
    INVALID_RESPONSE = "invalid_response"
    MISSING_HYDRATION = "missing_hydration"
    SCHEMA_MISMATCH = "schema_mismatch"
    PRIVATE = "private"
    ID_MISMATCH = "id_mismatch"
    UNSUPPORTED = "unsupported"
    PROCESS_ERROR = "process_error"
    UNAVAILABLE = "unavailable"
    READY = "ready"
    DISABLED = "disabled"
    POLICY = "policy"
    BUSY = "busy"
    EMPTY = "empty"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class LinkDiagnostic:
    stage: LinkStage
    reason: LinkReason
    duration_ms: int | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, LinkStage) or not isinstance(self.reason, LinkReason):
            raise ValueError("Diagnostic stage and reason must be fixed enums")
        if self.duration_ms is not None and (type(self.duration_ms) is not int or not 0 <= self.duration_ms <= 300_000):
            raise ValueError("Diagnostic duration is outside its bounds")
        if self.http_status is not None and (type(self.http_status) is not int or not 100 <= self.http_status <= 599):
            raise ValueError("Invalid diagnostic HTTP status")


@dataclass(frozen=True, slots=True)
class LinkExtraction[T]:
    value: T
    diagnostics: tuple[LinkDiagnostic, ...]


_collector: ContextVar[list[LinkDiagnostic] | None] = ContextVar("link_diagnostics", default=None)
_MAX_DIAGNOSTICS = 128


def record_link_diagnostic(
    stage: LinkStage,
    reason: LinkReason,
    *,
    duration_ms: int | None = None,
    http_status: int | None = None,
) -> None:
    events = _collector.get()
    if events is not None and len(events) < _MAX_DIAGNOSTICS:
        events.append(LinkDiagnostic(stage, reason, duration_ms, http_status))


def collect_link_diagnostics[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> LinkExtraction[T]:
    """Keep each worker invocation isolated, including nested calls and exceptions."""
    events: list[LinkDiagnostic] = []
    token = _collector.set(events)
    try:
        value = function(*args, **kwargs)
        return LinkExtraction(value, tuple(events))
    finally:
        _collector.reset(token)

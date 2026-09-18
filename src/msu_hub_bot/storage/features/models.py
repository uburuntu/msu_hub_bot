"""Typed envelopes and validation shared by feature persistence and workers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator


class FeatureError(RuntimeError):
    """Errors contain classifications, never stored payloads."""


class Conflict(FeatureError):
    def __init__(self) -> None:
        super().__init__("Feature record changed concurrently")


class OperationMismatch(FeatureError):
    def __init__(self) -> None:
        super().__init__("Feature operation ID was reused with a different request")


class FeatureProtocolError(FeatureError):
    def __init__(self) -> None:
        super().__init__("Invalid feature storage response")


class InvalidPayload(FeatureError):
    def __init__(self) -> None:
        super().__init__("Stored feature data cannot be validated or upgraded")


class FutureVersion(FeatureError):
    def __init__(self) -> None:
        super().__init__("Feature data requires a newer application version")


def identifier(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None:
        raise ValueError("Invalid feature identifier")
    return value


def key_text(value: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid feature key")
    return value


def timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Feature timestamps must be timezone-aware")
    return value.isoformat()


def _validate_key(value: str) -> str:
    return key_text(value)


class Payload(BaseModel):
    """Known fields are validated; unknown stored fields survive read/write cycles."""

    model_config = ConfigDict(extra="allow", hide_input_in_errors=True, validate_default=True, validate_assignment=True)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)


class Scope(Envelope):
    key: str
    owner: Literal["bot", "application"] = "bot"

    def __init__(self, key: str, owner: Literal["bot", "application"] = "bot") -> None:
        BaseModel.__init__(self, key=key, owner=owner)

    _key = field_validator("key")(_validate_key)


class RecordKey(Envelope):
    collection: str
    key: str

    def __init__(self, collection: str, key: str) -> None:
        BaseModel.__init__(self, collection=collection, key=key)

    _collection = field_validator("collection")(identifier)
    _key = field_validator("key")(_validate_key)


class RawRecord(Envelope):
    feature: str
    scope: Scope
    collection: str
    key: str
    etag: str
    payload_version: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    payload: dict[str, JsonValue]
    parent: str | None
    status: str | None
    expires_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    _identifiers = field_validator("feature", "collection")(identifier)
    _key = field_validator("key")(_validate_key)

    @field_validator("etag")
    @classmethod
    def valid_etag(cls, value: str) -> str:
        UUID(value)
        return value

    @field_validator("parent", "status")
    @classmethod
    def valid_optional_key(cls, value: str | None) -> str | None:
        return None if value is None else key_text(value)


@dataclass(frozen=True, slots=True)
class Record[M: Payload]:
    feature: str
    scope: Scope
    collection: str
    key: str
    etag: str
    value: M
    # The physical version can lag the in-memory model until the next mutation.
    payload_version: int
    parent: str | None
    status: str | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CommitResult:
    records: list[RawRecord]
    replayed: bool


class Job(Envelope):
    feature: str
    scope: Scope
    key: str
    kind: str
    record: RecordKey
    generation: Annotated[int, Field(strict=True, ge=1)]
    lease_token: str
    run_at: AwareDatetime
    attempts: Annotated[int, Field(strict=True, ge=1)]
    retry_until: AwareDatetime | None

    _identifiers = field_validator("feature", "kind")(identifier)
    _key = field_validator("key")(_validate_key)

    @field_validator("lease_token")
    @classmethod
    def valid_token(cls, value: str) -> str:
        UUID(value)
        return value

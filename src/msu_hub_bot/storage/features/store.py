"""Versioned feature documents over a small, authenticated transactional API."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol, cast

from pydantic import JsonValue, TypeAdapter, ValidationError

from .models import (
    CommitResult,
    Conflict,
    FeatureProtocolError,
    FutureVersion,
    InvalidPayload,
    OperationMismatch,
    Payload,
    RawRecord,
    Record,
    RecordKey,
    Scope,
    identifier,
    key_text,
    timestamp,
)

type Upgrade = Callable[[dict[str, JsonValue]], dict[str, JsonValue]]
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 256 * 1024


class FeatureBackend(Protocol):
    async def feature_request(self, operation: str, request: dict[str, JsonValue]) -> JsonValue: ...


class _Expiry(Enum):
    DEFAULT = "collection"


DEFAULT_RETENTION = _Expiry.DEFAULT


def _json_object(value: object, *, maximum: int = MAX_PAYLOAD_BYTES) -> dict[str, JsonValue]:
    try:
        result = _JSON_OBJECT.validate_python(value, strict=True)
        encoded = json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > maximum:
            raise ValueError("Feature data exceeds its size limit")
        return result
    except ValidationError, ValueError, TypeError, OverflowError, RecursionError:
        raise InvalidPayload() from None


def _decode_record(value: JsonValue) -> RawRecord:
    try:
        return RawRecord.model_validate(value)
    except ValidationError, ValueError, TypeError:
        raise FeatureProtocolError() from None


def _check_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidPayload()
    if isinstance(value, dict):
        for item in value.values():
            _check_finite(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _check_finite(item)


def _dump_payload(value: Payload) -> dict[str, JsonValue]:
    # JSON serialization can turn NaN into null. Inspect Python values first;
    # warning-as-error keeps malformed model_copy values out of stderr.
    try:
        _check_finite(value.model_dump(mode="python", warnings="error"))
        return _json_object(value.model_dump(mode="json", warnings="error"))
    except Exception:
        raise InvalidPayload() from None


class FeatureStore:
    def __init__(self, backend: FeatureBackend) -> None:
        self.backend = backend
        self._collections: dict[tuple[str, str], object] = {}

    async def check(self) -> None:
        result = await self.backend.feature_request("health", {})
        if not isinstance(result, dict) or type(result.get("version")) is not int or result["version"] != 1:
            raise FeatureProtocolError()

    def collection[M: Payload](
        self,
        feature: str,
        name: str,
        model: type[M],
        *,
        retention: timedelta | None,
        version: int = 1,
        upgrades: Mapping[int, Upgrade] | None = None,
    ) -> Collection[M]:
        result = Collection(self, feature, name, model, retention=retention, version=version, upgrades=upgrades)
        identity = (feature, name)
        if existing := self._collections.get(identity):
            registered = cast(Collection[M], existing)
            if (registered.model, registered.version, registered.retention, registered.upgrades) != (
                model,
                version,
                retention,
                result.upgrades,
            ):
                raise ValueError("Feature collection already has a different definition")
            return registered
        self._collections[identity] = result
        return result

    def transaction(self, feature: str, scope: Scope, *, operation_id: str) -> Transaction:
        return Transaction(self, feature, scope, operation_id=operation_id)


class Collection[M: Payload]:
    def __init__(
        self,
        store: FeatureStore,
        feature: str,
        name: str,
        model: type[M],
        *,
        retention: timedelta | None,
        version: int = 1,
        upgrades: Mapping[int, Upgrade] | None = None,
    ) -> None:
        self.store = store
        self.feature, self.name = identifier(feature), identifier(name)
        if not issubclass(model, Payload):
            raise TypeError("Feature models must inherit Payload")
        if type(version) is not int or not 1 <= version < 2**31:
            raise ValueError("Invalid feature model version")
        if retention is not None and (not isinstance(retention, timedelta) or retention <= timedelta(0)):
            raise ValueError("Feature retention must be positive or explicitly permanent")
        self.model, self.version, self.retention = model, version, retention
        self.upgrades = dict(upgrades or {})
        if any(type(v) is not int or not 1 <= v < version or not callable(f) for v, f in self.upgrades.items()):
            raise ValueError("Invalid feature upgrade chain")

    def _request(self, scope: Scope) -> dict[str, JsonValue]:
        return {"feature": self.feature, "scope": scope.model_dump(mode="json"), "collection": self.name}

    def decode(self, raw: RawRecord, scope: Scope) -> Record[M]:
        if (raw.feature, raw.scope, raw.collection) != (self.feature, scope, self.name):
            raise FeatureProtocolError()
        if raw.payload_version > self.version:
            raise FutureVersion()
        data = copy.deepcopy(_json_object(raw.payload))
        try:
            for version in range(raw.payload_version, self.version):
                data = _json_object(self.upgrades[version](data))
            value = self.model.model_validate(data)
            _dump_payload(value)
        except Exception:
            raise InvalidPayload() from None
        return Record(
            raw.feature,
            raw.scope,
            raw.collection,
            raw.key,
            raw.etag,
            value,
            raw.payload_version,
            raw.parent,
            raw.status,
            raw.expires_at,
            raw.created_at,
            raw.updated_at,
        )

    async def get(self, scope: Scope, key: str) -> Record[M] | None:
        value = await self.store.backend.feature_request("get", {**self._request(scope), "key": key_text(key)})
        if value is None:
            return None
        raw = _decode_record(value)
        if raw.key != key:
            raise FeatureProtocolError()
        return self.decode(raw, scope)

    async def list(
        self, scope: Scope, *, parent: str | None = None, status: str | None = None, after: str | None = None, limit: int = 100
    ) -> list[Record[M]]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Feature listing requires a bounded page")
        request = {
            **self._request(scope),
            "parent": None if parent is None else key_text(parent),
            "status": None if status is None else key_text(status, 64),
            "after": None if after is None else key_text(after),
            "limit": limit,
        }
        value = await self.store.backend.feature_request("list", request)
        if not isinstance(value, list) or len(value) > limit:
            raise FeatureProtocolError()
        result = [self.decode(_decode_record(item), scope) for item in value]
        keys = [record.key for record in result]
        if keys != sorted(set(keys)) or any(after is not None and key <= after for key in keys):
            raise FeatureProtocolError()
        if any((parent is not None and r.parent != parent) or (status is not None and r.status != status) for r in result):
            raise FeatureProtocolError()
        return result


class Transaction:
    """Freeze one bounded request; retry that same object after an uncertain response."""

    def __init__(self, store: FeatureStore, feature: str, scope: Scope, *, operation_id: str) -> None:
        self.store, self.feature, self.scope = store, identifier(feature), scope
        self.operation_id = key_text(operation_id, 128)
        self._guards: dict[tuple[str, str], str | None] = {}
        self._puts: list[dict[str, JsonValue]] = []
        self._deletes: list[dict[str, JsonValue]] = []
        self._jobs: list[dict[str, JsonValue]] = []
        self._cancel_jobs: list[str] = []
        self._frozen: dict[str, JsonValue] | None = None

    def _mutable(self) -> None:
        if self._frozen is not None:
            raise RuntimeError("A submitted feature transaction is immutable")

    def _guard(self, collection: str, key: str, etag: str | None) -> None:
        self._mutable()
        identity = (identifier(collection), key_text(key))
        if identity in self._guards and self._guards[identity] != etag:
            raise ValueError("Conflicting expectations for one feature record")
        self._guards[identity] = etag

    def expect[M: Payload](self, record: Record[M]) -> None:
        if (record.feature, record.scope) != (self.feature, self.scope):
            raise ValueError("Feature transactions cannot cross scopes")
        self._guard(record.collection, record.key, record.etag)

    def expect_absent(self, collection_name: str, key: str) -> None:
        self._guard(collection_name, key, None)

    def put[M: Payload](
        self,
        collection: Collection[M],
        key: str,
        value: M,
        *,
        parent: str | None = None,
        status: str | None = None,
        expires_at: datetime | None | _Expiry = DEFAULT_RETENTION,
    ) -> None:
        self._mutable()
        if collection.store is not self.store or collection.feature != self.feature or not isinstance(value, collection.model):
            raise ValueError("Feature mutation does not match its registered collection")
        if expires_at is DEFAULT_RETENTION:
            expires_at = None if collection.retention is None else datetime.now(UTC) + collection.retention
        expiry = None if expires_at is None else timestamp(expires_at)
        # Validate again: model_copy(update=...) deliberately bypasses Pydantic validation.
        try:
            payload = _dump_payload(value)
            checked = collection.model.model_validate(payload)
            payload = _dump_payload(checked)
        except Exception:
            raise InvalidPayload() from None
        self._puts.append(
            {
                "collection": collection.name,
                "key": key_text(key),
                "payload": payload,
                "payload_version": collection.version,
                "parent": None if parent is None else key_text(parent),
                "status": None if status is None else key_text(status, 64),
                "expires_at": expiry,
            }
        )

    def delete[M: Payload](self, record: Record[M]) -> None:
        self.expect(record)
        self._deletes.append({"collection": record.collection, "key": record.key})

    def schedule(
        self,
        key: str,
        kind: str,
        *,
        record: RecordKey,
        run_at: datetime,
        serial_key: str | None = None,
        retry_until: datetime | None = None,
    ) -> None:
        self._mutable()
        if retry_until is not None and retry_until < run_at:
            raise ValueError("Job retry deadline precedes its schedule")
        self._jobs.append(
            {
                "key": key_text(key),
                "kind": identifier(kind),
                "record": record.model_dump(mode="json"),
                "run_at": timestamp(run_at),
                "serial_key": None if serial_key is None else key_text(serial_key),
                "retry_until": None if retry_until is None else timestamp(retry_until),
            }
        )

    def cancel_job(self, key: str) -> None:
        self._mutable()
        self._cancel_jobs.append(key_text(key))

    def _freeze(self) -> dict[str, JsonValue]:
        if self._frozen is None:
            mutations = self._puts + self._deletes
            identities = [(str(item["collection"]), str(item["key"])) for item in mutations]
            if len(set(identities)) != len(identities) or any(identity not in self._guards for identity in identities):
                raise ValueError("Each changed record needs one guard and one mutation")
            job_keys = [str(job["key"]) for job in self._jobs] + self._cancel_jobs
            if len(set(job_keys)) != len(job_keys):
                raise ValueError("Each job can be changed only once per transaction")
            for job in self._jobs:
                target = cast(dict[str, JsonValue], job["record"])
                if (str(target["collection"]), str(target["key"])) not in self._guards:
                    raise ValueError("Scheduled work requires a guarded feature record")
            if len(self._guards) > 64 or len(mutations) + len(job_keys) > 64:
                raise ValueError("Feature transaction exceeds its batch limit")
            request: dict[str, JsonValue] = {
                "feature": self.feature,
                "scope": self.scope.model_dump(mode="json"),
                "operation_id": self.operation_id,
                "guards": [{"collection": c, "key": k, "etag": tag} for (c, k), tag in self._guards.items()],
                "puts": list(self._puts),
                "deletes": list(self._deletes),
                "jobs": list(self._jobs),
                "cancel_jobs": list(self._cancel_jobs),
            }
            self._frozen = copy.deepcopy(_json_object(request, maximum=MAX_REQUEST_BYTES))
        return copy.deepcopy(self._frozen)

    async def commit(self) -> CommitResult:
        result = await self.store.backend.feature_request("commit", self._freeze())
        if not isinstance(result, dict):
            raise FeatureProtocolError()
        outcome = result.get("outcome")
        if outcome == "conflict":
            raise Conflict()
        if outcome == "operation_mismatch":
            raise OperationMismatch()
        if outcome not in {"committed", "replayed"} or not isinstance(result.get("records"), list):
            raise FeatureProtocolError()
        records = [_decode_record(item) for item in cast(list[JsonValue], result["records"])]
        expected = {(str(item["collection"]), str(item["key"])) for item in self._puts}
        if (
            len(records) != len(expected)
            or {(r.collection, r.key) for r in records} != expected
            or any((r.feature, r.scope) != (self.feature, self.scope) for r in records)
        ):
            raise FeatureProtocolError()
        return CommitResult(records, outcome == "replayed")

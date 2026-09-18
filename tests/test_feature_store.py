"""Feature persistence contracts use scripted responses, never a second database."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from msu_hub_bot.storage.features.models import (
    Conflict,
    FeatureProtocolError,
    FutureVersion,
    InvalidPayload,
    OperationMismatch,
    Payload,
    RecordKey,
    Scope,
)
from msu_hub_bot.storage.features.store import FeatureStore
from msu_hub_bot.storage.supabase import RepositoryFailure, RepositoryUnavailable

NOW = datetime(2030, 1, 1, tzinfo=UTC)
SCOPE = Scope("chat:-100123")
CANARY = "synthetic-private-feature-value"


class Zone(Payload):
    name: str = "Europe/Moscow"


class Preferences(Payload):
    enabled: bool = True
    note: str | None = "default"
    zone: Zone = Zone()


class Counter(Payload):
    count: int


class Backend:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def feature_request(self, operation, request):
        self.calls.append((operation, deepcopy(request)))
        assert self.responses, "Unexpected feature request"
        expected_operation, response = self.responses.pop(0)
        assert operation == expected_operation
        if isinstance(response, BaseException):
            raise response
        return response


def raw(*, key="main", payload=None, **changes):
    return {
        "feature": "sample",
        "scope": SCOPE.model_dump(mode="json"),
        "collection": "preferences",
        "key": key,
        "etag": str(UUID(int=1)),
        "payload_version": 1,
        "payload": {} if payload is None else deepcopy(payload),
        "parent": None,
        "status": None,
        "expires_at": None,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        **changes,
    }


def setup(*responses, model=Preferences, **collection_options):
    backend = Backend(*responses)
    store = FeatureStore(backend)
    collection = store.collection("sample", "preferences", model, retention=None, **collection_options)
    return backend, store, collection


async def test_read_applies_adjacent_upgrades_without_writing_or_mutating_source():
    source = raw(payload={"old_count": 4, "future": {"secret": CANARY}})
    original = deepcopy(source)
    steps = []

    def one(data):
        steps.append(1)
        data["count"] = data.pop("old_count")
        return data

    def two(data):
        steps.append(2)
        data["count"] *= 10
        return data

    backend, _, collection = setup(("get", source), model=Counter, version=3, upgrades={1: one, 2: two})
    record = await collection.get(SCOPE, "main")
    assert record is not None and record.value.count == 40
    assert record.payload_version == 1 and record.etag == source["etag"]
    assert record.value.model_dump()["future"] == {"secret": CANARY}
    assert steps == [1, 2]
    assert source == original
    assert [operation for operation, _ in backend.calls] == ["get"]


async def test_ordinary_mutation_persists_upgrade_with_original_etag_and_unknown_fields():
    source = raw(payload={"old_count": 3, "future": {"keep": [1, 2]}})

    def upgrade(data):
        data["count"] = data.pop("old_count")
        return data

    committed = raw(payload={"count": 4, "future": {"keep": [1, 2]}}, payload_version=2, etag=str(UUID(int=2)))
    backend, store, collection = setup(
        ("get", source),
        ("commit", {"outcome": "committed", "records": [committed]}),
        model=Counter,
        version=2,
        upgrades={1: upgrade},
    )
    record = await collection.get(SCOPE, "main")
    record.value.count += 1
    tx = store.transaction("sample", SCOPE, operation_id="change-count")
    tx.expect(record)
    tx.put(collection, record.key, record.value)
    result = await tx.commit()
    request = backend.calls[-1][1]
    assert request["guards"] == [{"collection": "preferences", "key": "main", "etag": source["etag"]}]
    assert request["puts"][0]["payload_version"] == 2
    assert request["puts"][0]["payload"]["count"] == 4
    assert "old_count" not in request["puts"][0]["payload"]
    assert request["puts"][0]["payload"]["future"] == {"keep": [1, 2]}
    assert request["puts"][0]["expires_at"] is None
    assert result.records[0].etag == committed["etag"] and result.replayed is False


async def test_nested_unknown_fields_survive_known_field_update():
    value = {"enabled": True, "zone": {"name": "Europe/London", "future_nested": [CANARY]}, "future_outer": {"flag": False}}
    backend, store, collection = setup(("get", raw(payload=value)), ("commit", {"outcome": "committed", "records": [raw(payload=value)]}))
    record = await collection.get(SCOPE, "main")
    record.value.enabled = False
    tx = store.transaction("sample", SCOPE, operation_id="change-known-field")
    tx.expect(record)
    tx.put(collection, record.key, record.value)
    await tx.commit()
    payload = backend.calls[-1][1]["puts"][0]["payload"]
    assert payload["enabled"] is False
    assert payload["zone"]["future_nested"] == [CANARY]
    assert payload["future_outer"] == {"flag": False}


async def test_additive_default_does_not_rewrite_and_explicit_null_stays_null():
    backend, _, collection = setup(("get", raw()), ("get", raw(payload={"note": None})))
    missing = await collection.get(SCOPE, "main")
    explicit_null = await collection.get(SCOPE, "main")
    assert missing.value.note == "default"
    assert explicit_null.value.note is None
    assert [operation for operation, _ in backend.calls] == ["get", "get"]


async def test_future_version_is_not_guessed_or_overwritten():
    backend, _, collection = setup(("get", raw(payload={"unexpected": CANARY}, payload_version=2)))
    with pytest.raises(FutureVersion) as error:
        await collection.get(SCOPE, "main")
    assert CANARY not in str(error.value)
    assert [operation for operation, _ in backend.calls] == ["get"]


@pytest.mark.parametrize("failure", ["missing", "invalid_result", "value_error", "runtime_error"])
async def test_failed_upgrade_never_writes_defaults_or_exposes_payload(failure):
    source = raw(payload={"count": 1, "secret": CANARY})
    original = deepcopy(source)

    def upgrade(data):
        data["secret"] = "mutated locally"
        if failure == "invalid_result":
            return {"count": CANARY}
        if failure == "runtime_error":
            raise RuntimeError(CANARY)
        raise ValueError(CANARY)

    backend, _, collection = setup(("get", source), model=Counter, version=2, upgrades={} if failure == "missing" else {1: upgrade})
    with pytest.raises(InvalidPayload) as error:
        await collection.get(SCOPE, "main")
    assert CANARY not in str(error.value) and CANARY not in repr(error.value)
    assert source == original
    assert [operation for operation, _ in backend.calls] == ["get"]


async def test_current_version_does_not_rerun_old_upgrade():
    def obsolete(_):
        raise AssertionError("Current payload was upgraded again")

    _, _, collection = setup(("get", raw(payload={"count": 5}, payload_version=2)), model=Counter, version=2, upgrades={1: obsolete})
    record = await collection.get(SCOPE, "main")
    assert record.value.count == 5


@pytest.mark.parametrize(
    "changes",
    [
        {"feature": "other"},
        {"scope": {"key": "chat:-200", "owner": "bot"}},
        {"scope": {"key": SCOPE.key, "owner": "application"}},
        {"collection": "other"},
        {"key": "other"},
        {"etag": CANARY},
        {"payload_version": True},
        {"created_at": "2030-01-01T00:00:00"},
    ],
)
async def test_read_rejects_mismatched_or_invalid_envelope(changes):
    _, _, collection = setup(("get", raw(**changes)))
    with pytest.raises(FeatureProtocolError):
        await collection.get(SCOPE, "main")


async def test_get_missing_is_none_and_request_has_only_declared_scope():
    backend, _, collection = setup(("get", None))
    assert await collection.get(SCOPE, "absent") is None
    assert backend.calls == [
        ("get", {"feature": "sample", "scope": {"key": SCOPE.key, "owner": "bot"}, "collection": "preferences", "key": "absent"})
    ]


async def test_listing_passes_bounded_parent_status_and_cursor():
    backend, _, collection = setup(("list", [raw(key="b", parent="round", status="open"), raw(key="c", parent="round", status="open")]))
    records = await collection.list(SCOPE, parent="round", status="open", after="a", limit=2)
    assert [record.key for record in records] == ["b", "c"]
    assert backend.calls[-1][1] == {
        "feature": "sample",
        "scope": SCOPE.model_dump(mode="json"),
        "collection": "preferences",
        "parent": "round",
        "status": "open",
        "after": "a",
        "limit": 2,
    }


@pytest.mark.parametrize(
    "response",
    [
        [raw(key="b"), raw(key="a")],
        [raw(key="b"), raw(key="b")],
        [raw(key="a")],
        [raw(key="b"), raw(key="c"), raw(key="d")],
        {"records": []},
    ],
)
async def test_listing_rejects_pages_that_could_skip_or_repeat_records(response):
    _, _, collection = setup(("list", response))
    with pytest.raises(FeatureProtocolError):
        await collection.list(SCOPE, after="a", limit=2)


async def test_listing_rejects_valid_keys_in_descending_order():
    _, _, collection = setup(("list", [raw(key="c"), raw(key="b")]))
    with pytest.raises(FeatureProtocolError):
        await collection.list(SCOPE, after="a", limit=2)


@pytest.mark.parametrize("response", [[raw(key="b", parent="another")], [raw(key="b", parent="round", status="closed")]])
async def test_listing_does_not_accept_records_outside_filters(response):
    _, _, collection = setup(("list", response))
    with pytest.raises(FeatureProtocolError):
        await collection.list(SCOPE, parent="round", status="open")


@pytest.mark.parametrize("limit", [0, -1, 201, True, 1.5])
async def test_listing_invalid_limit_never_calls_backend(limit):
    backend, _, collection = setup()
    with pytest.raises(ValueError):
        await collection.list(SCOPE, limit=limit)
    assert backend.calls == []


async def test_uncertain_commit_retries_identical_frozen_request_and_operation_id():
    unavailable = RepositoryUnavailable(RepositoryFailure.TIMEOUT)
    backend, store, collection = setup(
        ("commit", unavailable),
        ("commit", {"outcome": "replayed", "records": [raw()]}),
    )
    tx = store.transaction("sample", SCOPE, operation_id="stable-create")
    tx.expect_absent(collection.name, "main")
    value = Preferences(future={"unchanged": 1})
    tx.put(collection, "main", value)
    with pytest.raises(RepositoryUnavailable) as error:
        await tx.commit()
    assert error.value is unavailable
    assert len(backend.calls) == 1
    value.future["unchanged"] = 2
    with pytest.raises(RuntimeError, match="immutable"):
        tx.cancel_job("other")
    result = await tx.commit()
    assert result.replayed is True
    assert backend.calls[0] == backend.calls[1]
    assert backend.calls[1][1]["puts"][0]["payload"]["future"] == {"unchanged": 1}


@pytest.mark.parametrize(("outcome", "error"), [("conflict", Conflict), ("operation_mismatch", OperationMismatch)])
async def test_commit_outcomes_are_explicit_and_never_automatically_retried(outcome, error):
    backend, store, collection = setup(("commit", {"outcome": outcome, "records": []}))
    tx = store.transaction("sample", SCOPE, operation_id="conflicting-create")
    tx.expect_absent(collection.name, "main")
    tx.put(collection, "main", Preferences())
    with pytest.raises(error):
        await tx.commit()
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"outcome": "unknown", "records": []},
        {"outcome": "committed", "records": []},
        {"outcome": "committed", "records": [raw(), raw()]},
        {"outcome": "committed", "records": [raw(key="other")]},
        {"outcome": "committed", "records": [raw(feature="other")]},
        {"outcome": "committed", "records": [raw(scope={"key": "chat:other", "owner": "bot"})]},
    ],
)
async def test_commit_rejects_incomplete_or_cross_scope_acknowledgement(response):
    _, store, collection = setup(("commit", response))
    tx = store.transaction("sample", SCOPE, operation_id="create")
    tx.expect_absent(collection.name, "main")
    tx.put(collection, "main", Preferences())
    with pytest.raises(FeatureProtocolError):
        await tx.commit()


async def test_forever_record_and_far_future_job_are_one_guarded_transaction():
    backend, store, collection = setup(("commit", {"outcome": "committed", "records": [raw()]}))
    due = datetime(2090, 1, 1, tzinfo=UTC)
    tx = store.transaction("sample", SCOPE, operation_id="future-work")
    tx.expect_absent(collection.name, "main")
    tx.put(collection, "main", Preferences(), expires_at=None)
    tx.schedule("deliver:main", "deliver", record=RecordKey(collection.name, "main"), run_at=due)
    await tx.commit()
    request = backend.calls[-1][1]
    assert request["puts"][0]["expires_at"] is None
    assert request["jobs"] == [
        {
            "key": "deliver:main",
            "kind": "deliver",
            "record": {"collection": "preferences", "key": "main"},
            "run_at": due.isoformat(),
            "serial_key": None,
            "retry_until": None,
        }
    ]
    assert len(backend.calls) == 1


async def test_expiry_default_can_be_overridden_by_explicit_forever_or_terminal_deadline():
    backend = Backend(("commit", {"outcome": "committed", "records": [raw(key=key) for key in ("pending", "finished", "temporary")]}))
    store = FeatureStore(backend)
    collection = store.collection("sample", "preferences", Preferences, retention=timedelta(days=1))
    tx = store.transaction("sample", SCOPE, operation_id="expiry-policy")
    tx.put(collection, "pending", Preferences(), expires_at=None)
    tx.put(collection, "finished", Preferences(), expires_at=NOW + timedelta(days=7))
    before = datetime.now(UTC) + timedelta(days=1)
    tx.put(collection, "temporary", Preferences())
    after = datetime.now(UTC) + timedelta(days=1)
    for key in ("pending", "finished", "temporary"):
        tx.expect_absent(collection.name, key)
    await tx.commit()
    puts = {value["key"]: value for value in backend.calls[-1][1]["puts"]}
    assert puts["pending"]["expires_at"] is None
    assert puts["finished"]["expires_at"] == (NOW + timedelta(days=7)).isoformat()
    assert before <= datetime.fromisoformat(puts["temporary"]["expires_at"]) <= after


async def test_read_guard_vote_and_coalesced_job_share_one_commit():
    backend, store, collection = setup(
        ("get", raw(status="open")), ("commit", {"outcome": "committed", "records": [raw(key="vote:42", parent="main")]})
    )
    round_ = await collection.get(SCOPE, "main")
    tx = store.transaction("sample", SCOPE, operation_id="vote-callback")
    tx.expect(round_)
    tx.expect_absent(collection.name, "vote:42")
    tx.put(collection, "vote:42", Preferences(), parent="main")
    tx.schedule("render:main", "render", record=RecordKey(collection.name, "main"), run_at=NOW, serial_key="render:main")
    await tx.commit()
    request = backend.calls[-1][1]
    assert request["guards"] == [
        {"collection": "preferences", "key": "main", "etag": round_.etag},
        {"collection": "preferences", "key": "vote:42", "etag": None},
    ]
    assert len(request["puts"]) == 1 and request["puts"][0]["parent"] == "main"
    assert request["jobs"][0]["record"]["key"] == "main"


async def test_delete_includes_guard_and_cancel_is_part_of_same_commit():
    backend, store, collection = setup(("get", raw()), ("commit", {"outcome": "committed", "records": []}))
    record = await collection.get(SCOPE, "main")
    tx = store.transaction("sample", SCOPE, operation_id="delete-and-cancel")
    tx.delete(record)
    tx.cancel_job("deliver:main")
    await tx.commit()
    request = backend.calls[-1][1]
    assert request["guards"][0]["etag"] == record.etag
    assert request["deletes"] == [{"collection": "preferences", "key": "main"}]
    assert request["cancel_jobs"] == ["deliver:main"]


@pytest.mark.parametrize("scenario", ["unguarded", "duplicate_mutation", "unguarded_job", "duplicate_job", "too_many"])
async def test_invalid_transaction_never_reaches_backend(scenario):
    backend, store, collection = setup()
    tx = store.transaction("sample", SCOPE, operation_id="invalid-transaction")
    if scenario == "unguarded":
        tx.put(collection, "main", Preferences())
    elif scenario == "duplicate_mutation":
        tx.expect_absent(collection.name, "main")
        tx.put(collection, "main", Preferences())
        tx.put(collection, "main", Preferences())
    elif scenario == "unguarded_job":
        tx.schedule("work", "deliver", record=RecordKey(collection.name, "main"), run_at=NOW)
    elif scenario == "duplicate_job":
        tx.expect_absent(collection.name, "main")
        tx.put(collection, "main", Preferences())
        tx.schedule("work", "deliver", record=RecordKey(collection.name, "main"), run_at=NOW)
        tx.cancel_job("work")
    else:
        for index in range(65):
            tx.expect_absent(collection.name, str(index))
            tx.put(collection, str(index), Preferences())
    with pytest.raises(ValueError):
        await tx.commit()
    assert backend.calls == []


@pytest.mark.parametrize("extra", ["🦀" * 17000, float("nan"), float("inf"), object()])
def test_payload_bounds_and_json_validation_do_not_expose_stored_values(extra):
    backend, store, collection = setup()
    tx = store.transaction("sample", SCOPE, operation_id="invalid-payload")
    with pytest.raises(InvalidPayload) as error:
        tx.put(collection, "main", Preferences(private=CANARY, future=extra))
    assert CANARY not in str(error.value)
    assert backend.calls == []


def test_model_copy_cannot_bypass_validation_at_storage_boundary(recwarn):
    backend, store, collection = setup(model=Counter)
    tx = store.transaction("sample", SCOPE, operation_id="copied-invalid")
    invalid = Counter(count=1).model_copy(update={"count": CANARY})
    with pytest.raises(InvalidPayload):
        tx.put(collection, "main", invalid)
    assert backend.calls == []
    assert not recwarn.list, "Invalid private payloads must not escape in serializer warnings"


async def test_request_has_a_total_bound_even_when_each_record_fits():
    backend, store, collection = setup()
    tx = store.transaction("sample", SCOPE, operation_id="too-large-batch")
    for index in range(5):
        tx.expect_absent(collection.name, str(index))
        tx.put(collection, str(index), Preferences(future="x" * 60000))
    with pytest.raises(InvalidPayload):
        await tx.commit()
    assert backend.calls == []


def test_collection_registration_is_explicit_and_cannot_change_silently():
    _, store, collection = setup()
    assert store.collection("sample", "preferences", Preferences, retention=None) is collection
    with pytest.raises(ValueError, match="different definition"):
        store.collection("sample", "preferences", Preferences, retention=timedelta(days=1))
    with pytest.raises(ValueError, match="different definition"):
        store.collection("sample", "preferences", Preferences, retention=None, version=2)


@pytest.mark.parametrize("retention", [timedelta(0), timedelta(seconds=-1), 1, True])
def test_invalid_retention_cannot_become_implicit_forever(retention):
    with pytest.raises(ValueError):
        FeatureStore(Backend()).collection("sample", "preferences", Preferences, retention=retention)


async def test_cross_scope_guard_and_foreign_store_mutation_are_rejected():
    _, store, collection = setup(("get", raw()))
    record = await collection.get(SCOPE, "main")
    tx = store.transaction("sample", Scope("chat:other"), operation_id="wrong-scope")
    with pytest.raises(ValueError, match="cross scopes"):
        tx.expect(record)
    other_store = FeatureStore(Backend())
    tx = other_store.transaction("sample", SCOPE, operation_id="wrong-store")
    with pytest.raises(ValueError, match="registered collection"):
        tx.put(collection, "main", Preferences())


@pytest.mark.parametrize("value", ["", "bad\x00key", "x" * 257])
def test_scope_rejects_unbounded_or_control_character_keys(value):
    with pytest.raises((ValueError, ValidationError)):
        Scope(value)


@pytest.mark.parametrize("version", [True, 0, 2, "1", None])
async def test_foundation_health_requires_exact_contract_version(version):
    store = FeatureStore(Backend(("health", {"version": version})))
    with pytest.raises(FeatureProtocolError):
        await store.check()

"""Feature transactions and leases against the actual PostgreSQL implementation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_postgres_storage import PRINCIPAL, STRANGER, literal, namespace_snapshot

SCOPE = {"key": "chat:-101", "owner": "bot"}


def when(days=0, seconds=0):
    return (datetime.now(UTC) + timedelta(days=days, seconds=seconds)).isoformat()


def request(**changes):
    return {"feature": "quiz", "scope": SCOPE, **changes}


def transaction(**changes):
    return request(operation_id=str(uuid4()), guards=[], puts=[], deletes=[], jobs=[], cancel_jobs=[], **changes)


def put(key="round", **changes):
    return {
        "collection": "rounds",
        "key": key,
        "payload": {"question": "Synthetic", "unknown": {"future": None}},
        "payload_version": 1,
        "parent": None,
        "status": "active",
        "expires_at": None,
        **changes,
    }


def guard(key="round", etag=None, collection="rounds"):
    return {"collection": collection, "key": key, "etag": etag}


def job(key="finish", record_key="round", **changes):
    return {
        "key": key,
        "kind": "finish",
        "record": {"collection": "rounds", "key": record_key},
        "run_at": when(seconds=-1),
        "serial_key": None,
        "retry_until": None,
        **changes,
    }


def call(db, operation, value, principal=PRINCIPAL):
    return db.rpc("feature_" + operation, literal(value), principal=principal)


def create(db, key="round", *, jobs=None, scope=SCOPE, **changes):
    tx = transaction()
    tx.update(scope=scope, guards=[guard(key)], puts=[put(key, **changes)], jobs=jobs or [])
    return call(db, "commit", tx)["records"][0]


def get(db, key="round", **changes):
    return call(db, "get", request(collection="rounds", key=key, **changes))


def claim(db, *, principal=PRINCIPAL, **changes):
    return call(
        db, "claim_jobs", {"handlers": [{"feature": "quiz", "kind": "finish"}], "limit": 10, "lease_seconds": 60, **changes}, principal
    )


def status(db, claimed, action="check", **changes):
    return call(
        db,
        "job_status",
        {**{key: claimed[key] for key in ("feature", "scope", "key", "generation", "lease_token")}, "action": action, **changes},
    )["current"]


def exercise_feature_migration(db, migration):
    db.run("INSERT INTO msu_hub_private.schema_migrations(version) VALUES(99);")
    rejected = db.run(migration, check=False)
    assert rejected.returncode and "requires schema revision 4" in rejected.stderr
    db.run("DELETE FROM msu_hub_private.schema_migrations WHERE version=99;")
    before = namespace_snapshot(db, "msu_hub_private")
    interrupted = migration.replace("INSERT INTO msu_hub_private.schema_migrations(version) VALUES(5);", "SELECT 1/0;")
    assert db.run(interrupted, check=False).returncode
    assert namespace_snapshot(db, "msu_hub_private") == before
    db.run(migration)
    after = namespace_snapshot(db, "msu_hub_private")
    assert {key: value for key, value in before["rows"].items() if key != "schema_migrations"} == {
        key: value for key, value in after["rows"].items() if key != "schema_migrations"
    }
    after_relations = {row[0]: row for row in after["relations"]}
    assert all(after_relations[row[0]] == row for row in before["relations"])
    after_functions = {row["oid"]: row for row in after["functions"]}
    assert all(after_functions[row["oid"]] == row for row in before["functions"])
    assert db.rpc("health") == {"schema_version": 1, "bot_id": 999}
    assert call(db, "health", {}) == {"version": 1}
    return True


def test_feature_migration_is_additive_atomic_and_preserves_old_rpc_bodies(postgres):
    assert postgres.feature_upgrade


def test_records_preserve_envelope_unknown_fields_and_application_scope(db):
    first = create(db)
    assert first == get(db)
    assert first["scope"] == SCOPE and first["expires_at"] is None
    assert first["payload"]["unknown"] == {"future": None}
    assert set(first) == {
        "feature",
        "scope",
        "collection",
        "key",
        "etag",
        "payload_version",
        "payload",
        "parent",
        "status",
        "expires_at",
        "created_at",
        "updated_at",
    }
    db.run(f"INSERT INTO msu_hub_private.principals(auth_user_id,bot_id) VALUES('{STRANGER}',888);")
    assert call(db, "get", request(collection="rounds", key="round"), STRANGER) is None
    shared_scope = {**SCOPE, "owner": "application"}
    shared = create(db, scope=shared_scope)
    assert call(db, "get", request(scope=shared_scope, collection="rounds", key="round"), STRANGER) == shared
    assert get(db, scope={**SCOPE, "key": "chat:-202"}) is None


def test_cas_atomic_read_only_guard_vote_and_job_replay(db):
    original = create(db)
    tx = transaction()
    tx.update(
        guards=[guard(etag=original["etag"]), guard("101", collection="votes")],
        puts=[put("101", collection="votes", parent="round", payload={"choice": 2})],
        jobs=[job("render")],
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: call(db, "commit", tx), range(4)))
    assert sorted(result["outcome"] for result in results) == ["committed", "replayed", "replayed", "replayed"]
    assert all(result["records"] == results[0]["records"] for result in results)
    assert get(db) == original
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_jobs;") == 1
    assert db.value("SELECT generation FROM msu_hub_private.feature_jobs;") == 1
    tx["puts"][0]["payload"]["choice"] = 3
    assert call(db, "commit", tx)["outcome"] == "operation_mismatch"
    tx["operation_id"] = str(uuid4())
    assert call(db, "commit", tx)["outcome"] == "conflict"
    assert db.value("SELECT payload FROM msu_hub_private.feature_records WHERE collection='votes';") == {"choice": 2}


def test_concurrent_round_guard_prevents_late_vote_and_partial_job(db):
    old = create(db)
    close = transaction()
    close.update(guards=[guard(etag=old["etag"])], puts=[put(status="finished")])
    assert call(db, "commit", close)["outcome"] == "committed"
    vote = transaction()
    vote.update(guards=[guard(etag=old["etag"]), guard("101", collection="votes")], puts=[put("101", collection="votes")], jobs=[job()])
    assert call(db, "commit", vote) == {"outcome": "conflict", "records": []}
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_records WHERE collection='votes';") == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_jobs;") == 0


def test_same_etag_racing_writers_allow_one_complete_transaction(db):
    old = create(db)
    values = []
    for number in range(8):
        tx = transaction()
        tx.update(guards=[guard(etag=old["etag"])], puts=[put(payload={"number": number})], jobs=[job()])
        values.append(tx)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda tx: call(db, "commit", tx), values))
    assert [result["outcome"] for result in results].count("committed") == 1
    assert db.value("SELECT generation FROM msu_hub_private.feature_jobs;") == 1


def test_receipt_replays_original_result_and_expiry_does_not_bypass_cas(db):
    tx = transaction()
    tx.update(guards=[guard()], puts=[put()])
    created = call(db, "commit", tx)
    assert db.value(
        "SELECT to_jsonb(bool_and(NOT r ? 'payload')) FROM msu_hub_private.feature_operations o CROSS JOIN LATERAL jsonb_array_elements(o.result->'records') r;"
    )
    update = transaction()
    update.update(guards=[guard(etag=created["records"][0]["etag"])], puts=[put(payload={"changed": True})])
    call(db, "commit", update)
    replay = call(db, "commit", tx)
    assert replay["outcome"] == "replayed" and replay["records"] == created["records"]
    db.run("UPDATE msu_hub_private.feature_operations SET expires_at=now()-interval '1 second';")
    assert call(db, "commit", tx)["outcome"] == "conflict"
    assert get(db)["payload"] == {"changed": True}


def test_key_pagination_and_parent_status_filters_use_unicode_codepoint_order(db):
    for key in ["а", "A", "😀", "a", "á", "0", "_", "z"]:
        create(db, key, parent="one" if key in {"A", "a"} else "two", status="active" if key != "a" else "done")
    seen = []
    after = None
    while page := call(db, "list", request(collection="rounds", after=after, limit=3)):
        seen.extend(item["key"] for item in page)
        after = page[-1]["key"]
    assert seen == sorted(seen) and len(seen) == 8
    assert [r["key"] for r in call(db, "list", request(collection="rounds", parent="one", limit=200))] == ["A", "a"]
    assert [r["key"] for r in call(db, "list", request(collection="rounds", parent="one", status="active", limit=200))] == ["A"]


def test_expired_records_hidden_forever_not_extended_and_uuid_prevents_aba(db):
    old = create(db, expires_at=when(seconds=60))
    forever = create(db, "forever")
    assert get(db)["expires_at"] == old["expires_at"]
    db.run("UPDATE msu_hub_private.feature_records SET expires_at=now()-interval '1 second' WHERE key='round';")
    assert get(db) is None
    assert call(db, "list", request(collection="rounds", limit=200)) == [forever]
    fresh = create(db)
    assert fresh["etag"] != old["etag"] and fresh["created_at"] > old["created_at"]
    stale = transaction()
    stale.update(guards=[guard(etag=old["etag"])], puts=[put(payload={"bad": True})])
    assert call(db, "commit", stale)["outcome"] == "conflict"


@pytest.mark.parametrize(
    "problem",
    [
        "no_guard",
        "missing_etag",
        "duplicate_guard",
        "duplicate_put",
        "put_delete",
        "bad_job",
        "bad_time",
        "no_expiry",
        "version_down",
        "null_payload",
        "large_payload",
        "job_and_cancel",
        "numeric_key",
    ],
)
def test_invalid_transaction_rolls_back_records_jobs_and_receipts(db, problem):
    original = create(db, payload_version=2)
    tx = transaction()
    tx.update(guards=[guard(etag=original["etag"])], puts=[put(payload_version=2)], jobs=[job()])
    if problem == "no_guard":
        tx["guards"] = []
    elif problem == "missing_etag":
        del tx["guards"][0]["etag"]
    elif problem == "duplicate_guard":
        tx["guards"] *= 2
    elif problem == "duplicate_put":
        tx["puts"] *= 2
    elif problem == "put_delete":
        tx["deletes"] = [{"collection": "rounds", "key": "round"}]
    elif problem == "bad_job":
        tx["jobs"][0]["record"]["key"] = "missing"
    elif problem == "bad_time":
        tx["jobs"][0]["run_at"] = "infinity"
    elif problem == "no_expiry":
        del tx["puts"][0]["expires_at"]
    elif problem == "version_down":
        tx["puts"][0]["payload_version"] = 1
    elif problem == "null_payload":
        tx["puts"][0]["payload"] = None
    elif problem == "large_payload":
        tx["puts"][0]["payload"] = {"text": "x" * 65536}
    elif problem == "job_and_cancel":
        tx["cancel_jobs"] = ["finish"]
    elif problem == "numeric_key":
        tx["puts"][0]["key"] = 1
    outcome = db.run(f"SELECT msu_hub_api.feature_commit_v1({literal(tx)});", principal=PRINCIPAL, check=False)
    assert outcome.returncode
    assert get(db) == original
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_operations;") == 1
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_jobs;") == 0


def test_cancelling_jobs_and_deleting_record_is_one_transaction(db):
    original = create(db, jobs=[job()])
    tx = transaction()
    tx.update(guards=[guard(etag=original["etag"])], deletes=[{"collection": "rounds", "key": "round"}])
    assert db.run(f"SELECT msu_hub_api.feature_commit_v1({literal(tx)});", principal=PRINCIPAL, check=False).returncode
    assert get(db) == original
    tx["cancel_jobs"] = ["finish"]
    assert call(db, "commit", tx)["outcome"] == "committed"
    assert get(db) is None and claim(db) == []


@pytest.mark.parametrize("transition", ["cancel", "reschedule"])
def test_expired_recreation_requires_transitioning_all_unfinished_jobs(db, transition):
    create(db, jobs=[job()], expires_at=when(days=1))
    old = claim(db)[0]
    db.run("UPDATE msu_hub_private.feature_records SET expires_at=now()-interval '1 second';")
    tx = transaction()
    tx.update(guards=[guard()], puts=[put(payload={"replacement": True})])
    assert db.run(f"SELECT msu_hub_api.feature_commit_v1({literal(tx)});", principal=PRINCIPAL, check=False).returncode
    assert get(db) is None and status(db, old)
    tx["cancel_jobs" if transition == "cancel" else "jobs"] = ["finish"] if transition == "cancel" else [job()]
    assert call(db, "commit", tx)["outcome"] == "committed"
    assert get(db)["payload"] == {"replacement": True}
    assert not status(db, old, "complete")
    assert bool(claim(db)) == (transition == "reschedule")


def test_maximum_multibyte_scope_parent_and_key_fit_all_indexes(db):
    scope = {"key": "".join(chr(0x10000 + n) for n in range(256)), "owner": "bot"}
    key = "".join(chr(0x10100 + n) for n in range(256))
    parent = "".join(chr(0x10200 + n) for n in range(256))
    record = create(db, key, scope=scope, parent=parent)
    assert call(db, "list", request(scope=scope, collection="rounds", parent=parent, limit=1)) == [record]


def test_concurrent_claim_has_one_owner_and_reclaims_only_expired_lease(db):
    create(db, jobs=[job()])
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = [claimed for result in pool.map(lambda _: claim(db), range(8)) for claimed in result]
    assert len(claims) == 1 and claims[0]["attempts"] == 1
    first = claims[0]
    assert status(db, first)
    assert not status(db, {**first, "lease_token": str(uuid4())})
    assert status(db, first, "renew", lease_seconds=120)
    assert claim(db) == []
    db.run("UPDATE msu_hub_private.feature_jobs SET lease_until=now()-interval '1 second';")
    assert not status(db, first)
    second = claim(db)[0]
    assert second["lease_token"] != first["lease_token"] and second["attempts"] == 2
    assert not status(db, first, "complete")
    assert status(db, second, "complete")
    assert claim(db) == []


@pytest.mark.parametrize("terminal_action", ["complete", "retry", "hold", "expire"])
def test_coalescing_and_cancel_fence_old_workers_without_losing_new_generation(db, terminal_action):
    record = create(db, jobs=[job()])
    old = claim(db)[0]
    tx = transaction()
    tx.update(guards=[guard(etag=record["etag"])], jobs=[job()])
    call(db, "commit", tx)
    assert not status(db, old) and not status(db, old, "renew", lease_seconds=60)
    assert claim(db) == []
    kwargs = {"run_at": when()} if terminal_action == "retry" else {}
    assert not status(db, old, terminal_action, **kwargs)
    new = claim(db)[0]
    assert new["generation"] == old["generation"] + 1 and new["attempts"] == 1
    assert not status(db, old, "complete") and status(db, new)
    cancel = transaction()
    cancel["cancel_jobs"] = ["finish"]
    call(db, "commit", cancel)
    reschedule = transaction()
    reschedule.update(guards=[guard(etag=record["etag"])], jobs=[job()])
    call(db, "commit", reschedule)
    assert not status(db, new, "complete")
    third = claim(db)[0]
    assert third["generation"] == new["generation"] + 2
    assert status(db, third, "complete")


def test_serial_jobs_preserve_order_through_retries_holds_and_rescheduling(db):
    record = create(db, jobs=[job("first", serial_key="scores"), job("second", serial_key="scores"), job("other", serial_key="other")])
    claims = {value["key"]: value for value in claim(db)}
    assert set(claims) == {"first", "other"}
    assert status(db, claims["first"], "retry", run_at=when(days=1))
    assert status(db, claims["other"], "complete")
    assert claim(db) == []
    tx = transaction()
    tx.update(guards=[guard(etag=record["etag"])], jobs=[job("first", serial_key="scores")])
    call(db, "commit", tx)
    first = claim(db)[0]
    assert first["key"] == "first" and status(db, first, "hold")
    assert claim(db) == []
    tx["operation_id"] = str(uuid4())
    call(db, "commit", tx)
    first = claim(db)[0]
    assert first["key"] == "first" and status(db, first, "complete")
    assert claim(db)[0]["key"] == "second"
    tx["operation_id"] = str(uuid4())
    tx["jobs"][0]["serial_key"] = "different"
    assert db.run(f"SELECT msu_hub_api.feature_commit_v1({literal(tx)});", principal=PRINCIPAL, check=False).returncode


def test_job_claim_respects_feature_bot_scope_and_registered_kind(db):
    db.run(f"INSERT INTO msu_hub_private.principals(auth_user_id,bot_id) VALUES('{STRANGER}',888);")
    create(db, jobs=[job()])
    create(db, jobs=[job()], scope={**SCOPE, "owner": "application"})
    assert claim(db, handlers=[{"feature": "other", "kind": "finish"}]) == []
    shared = claim(db, principal=STRANGER)
    assert len(shared) == 1 and shared[0]["scope"]["owner"] == "application"
    own = claim(db)
    assert len(own) == 1 and own[0]["scope"]["owner"] == "bot"
    assert not call(
        db, "job_status", {**{k: own[0][k] for k in ("feature", "scope", "key", "generation", "lease_token")}, "action": "check"}, STRANGER
    )["current"]


def test_retention_protects_held_pending_dependencies_and_preserves_forever(db):
    create(db, jobs=[job(run_at=when(days=-2), retry_until=when(days=-1))], expires_at=when(days=1))
    create(db, "forever")
    create(db, "expired", expires_at=when(days=-1))
    claimed = claim(db)[0]
    assert status(db, claimed, "hold")
    db.run("UPDATE msu_hub_private.feature_records SET expires_at=now()-interval '1 day' WHERE key='round';")
    db.run("UPDATE msu_hub_private.feature_operations SET expires_at=now()-interval '1 day';")
    assert get(db) is None
    assert db.value("SELECT msu_hub_private.retain_features(100);") == {"records": 1, "jobs": 0, "operations": 3}
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_records;") == 2
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_jobs WHERE state='held';") == 1
    tx = transaction()
    tx["cancel_jobs"] = ["finish"]
    call(db, "commit", tx)
    assert db.value("SELECT msu_hub_private.retain_features(100);")["records"] == 1
    assert get(db, "forever") is not None
    assert db.value("SELECT msu_hub_private.retain_features(100);")["jobs"] == 0
    db.run("UPDATE msu_hub_private.feature_jobs SET terminal_at=now()-interval '8 days';")
    assert db.value("SELECT msu_hub_private.retain_features(100);")["jobs"] == 1


def test_retention_is_bounded_independent_and_does_not_change_messages_contract(db):
    for index in range(3):
        create(db, str(index), expires_at=when(days=-1))
    db.run("UPDATE msu_hub_private.feature_operations SET expires_at=now()-interval '1 day';")
    assert db.value("SELECT msu_hub_private.retain_features(1);") == {"records": 1, "jobs": 0, "operations": 1}
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_records;") == 2
    assert set(db.value("SELECT msu_hub_private.retain_messages();")) == {
        "messages",
        "updates",
        "reaction_actors",
        "reaction_counts",
        "cutoff",
    }
    for arguments in ("0", "10001", "NULL", "1,'infinity'", "1,NULL"):
        assert db.run(f"SELECT msu_hub_private.retain_features({arguments});", check=False).returncode


@pytest.mark.parametrize("operation", ["get", "list", "commit", "claim_jobs", "job_status", "health"])
def test_all_feature_rpcs_require_authorized_enabled_principal(db, operation):
    sql = f"SELECT msu_hub_api.feature_{operation}_v1('{{}}');"
    denied = db.run(sql, principal=STRANGER, check=False)
    assert denied.returncode and "not authorized" in denied.stderr
    assert db.run("SET ROLE anon; " + sql, check=False).returncode
    db.run("UPDATE msu_hub_private.principals SET enabled=false;")
    assert "not authorized" in db.run(sql, principal=PRINCIPAL, check=False).stderr


def test_feature_private_tables_helpers_and_sequence_are_not_directly_accessible(db):
    for table in ("feature_records", "feature_jobs", "feature_operations"):
        assert db.run(f"SELECT * FROM msu_hub_private.{table};", principal=PRINCIPAL, check=False).returncode
    for sql in (
        "SELECT msu_hub_private.retain_features();",
        "SELECT nextval('msu_hub_private.feature_jobs_sequence_seq');",
        "SELECT msu_hub_private.feature_text('\"key\"',256);",
    ):
        assert db.run(sql, principal=PRINCIPAL, check=False).returncode
    assert (
        db.value("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='msu_hub_private' AND c.relname LIKE 'feature_%' AND c.relkind='r'
          AND (NOT c.relrowsecurity OR c.relowner <> 'msu_hub_owner'::regrole);""")
        == 0
    )
    assert db.value("SELECT count(*) FROM msu_hub_private.mutation_journal;") == 0


async def test_python_facade_and_worker_roundtrip_persist_across_new_instances(db):
    from msu_hub_bot.storage.features import FeatureStore, FeatureWorker, Payload, RecordKey, Scope

    class Counter(Payload):
        count: int

    class Backend:
        async def feature_request(self, operation, value):
            return await asyncio.to_thread(call, db, operation, value)

    scope = Scope("chat:-101")
    store = FeatureStore(Backend())
    await store.check()
    rounds = store.collection("quiz", "rounds", Counter, retention=None)
    initial = store.transaction("quiz", scope, operation_id="create-round")
    initial.expect_absent("rounds", "round")
    initial.put(rounds, "round", Counter(count=0, future={"preserved": True}))
    initial.schedule("finish", "finish", record=RecordKey("rounds", "round"), run_at=datetime.now(UTC) - timedelta(seconds=1))
    result = await initial.commit()
    replay = await initial.commit()
    assert replay.replayed and result.records == replay.records

    restarted = FeatureStore(Backend())
    restored_rounds = restarted.collection("quiz", "rounds", Counter, retention=None)
    loaded = await restored_rounds.get(scope, "round")
    assert loaded is not None and loaded.etag == result.records[0].etag
    assert await restored_rounds.list(scope) == [loaded]
    worker = FeatureWorker(restarted)

    async def finish(context):
        assert await context.current() and await context.renew()
        record = await restored_rounds.get(context.job.scope, context.job.record.key)
        assert record is not None
        change = restarted.transaction("quiz", scope, operation_id="finish-round")
        change.expect(record)
        change.put(restored_rounds, record.key, record.value.model_copy(update={"count": record.value.count + 1}))
        await change.commit()

    worker.register("quiz", "finish", finish)
    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    final = await rounds.get(scope, "round")
    assert final is not None and final.value.model_dump() == {"count": 1, "future": {"preserved": True}}
    assert final.etag != loaded.etag and final.expires_at is None
    assert db.value("SELECT to_jsonb(state) FROM msu_hub_private.feature_jobs;") == "complete"


@pytest.mark.parametrize("feature", ["chess", "geoguess"])
async def test_quiz_restart_settlement_and_cleanup_use_real_feature_transactions(db, monkeypatch, feature):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from aiogram.methods import SendPhoto
    from quiz_helpers import GameSession, PHOTO, PNG, PUZZLE, click, edits, restart, score_rows, score_values, settle, start, text_of
    from telegram_helpers import make_message

    from msu_hub_bot.games import definitions
    from msu_hub_bot.games.quiz import RESULT_TTL
    from msu_hub_bot.telegram.wrapper import BotWrapper

    class Backend:
        now = datetime.now(UTC)

        def __init__(self):
            self.score_requests = []
            self.lost = False

        async def feature_request(self, operation, value):
            from msu_hub_bot.storage.supabase import RepositoryFailure, RepositoryUnavailable

            result = await asyncio.to_thread(call, db, operation, value)
            if operation == "commit" and any(put["collection"] == "scores" for put in value["puts"]):
                self.score_requests.append((value, result["outcome"]))
                if not self.lost:
                    self.lost = True
                    raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
            return result

    monkeypatch.setattr("msu_hub_bot.games.quiz.EDIT_INTERVAL", 0)
    monkeypatch.setattr(definitions, "random_puzzle", AsyncMock(return_value=PUZZLE))
    monkeypatch.setattr(definitions, "random_photo", AsyncMock(return_value=PHOTO))
    monkeypatch.setattr(definitions, "render_board", Mock(return_value=PNG))
    backend = Backend()
    session = GameSession(backend)
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    rig = SimpleNamespace(
        feature=feature,
        backend=backend,
        session=session,
        bot=bot,
        message=make_message(bot, message_id=10, date=backend.now, is_topic_message=True, message_thread_id=17),
    )
    restart(rig)
    try:
        record = await start(rig)
        assert record is not None and record.value.question is not None
        question = record.value.question.model_dump()
        message_id = record.value.message_id
        for uid in range(42, 107):
            choice = (record.value.question.answer + (1 if uid == 43 else 0)) % 6
            await click(rig, record, choice, user_id=uid)
        rig.worker.stop()
        restart(rig)
        definitions.random_puzzle.side_effect = AssertionError("A restored game must not fetch another question")
        definitions.random_photo.side_effect = AssertionError("A restored game must not fetch another question")
        restored = await rig.quiz.round(feature, record.value.chat_id, record.key)
        assert restored.value.question.model_dump() == question
        assert restored.value.message_id == message_id and restored.value.thread_id == 17
        voters = await rig.quiz.votes(feature, record.scope, record.key)
        assert {v.value.user_id: v.value.choice for v in voters} == {
            uid: (question["answer"] + (1 if uid == 43 else 0)) % 6 for uid in range(42, 107)
        }
        await click(rig, restored, "finish")
        await settle(rig)
        closed = await rig.quiz.round(feature, record.value.chat_id, record.key)
        assert closed.value.phase == "closed" and closed.value.score_status == "recorded"
        assert closed.value.message_id == message_id
        assert closed.value.score_count == 65 and closed.value.score_cursor is not None
        expected_scores = {uid: 0 if uid == 43 else 1 for uid in range(42, 107)}
        assert await score_values(rig, closed.value.score_day) == expected_scores
        assert [outcome for _, outcome in backend.score_requests] == ["committed", "replayed", "committed", "committed"]
        assert backend.score_requests[0][0] == backend.score_requests[1][0]
        assert [
            sum(put["collection"] == "scores" for put in request["puts"])
            for request, outcome in backend.score_requests
            if outcome == "committed"
        ] == [30, 30, 5]
        assert "Угадали 64 из 65" in text_of(edits(rig)[-1])
        assert {method.message_id for method in edits(rig)} == {message_id}
        assert len([method for method in session.methods if isinstance(method, SendPhoto)]) == 1

        # Advance the service clock and make only this synthetic cleanup job due.
        backend.now = closed.value.closed_at + RESULT_TTL + timedelta(seconds=1)
        db.run(f"""UPDATE msu_hub_private.feature_jobs SET run_at=clock_timestamp()-interval '1 second'
            WHERE feature='{feature}' AND record_key='{record.key}' AND kind='cleanup';""")
        await settle(rig)
        assert await rig.quiz.round(feature, record.value.chat_id, record.key) is None
        assert await rig.quiz.votes(feature, record.scope, record.key) == []
        assert db.value("SELECT count(*) FROM msu_hub_private.feature_jobs WHERE terminal_at IS NULL;") == 0
        chat = await rig.quiz.collections[feature].chats.get(record.scope, "state")
        assert chat.value.recent == [record.value.question.identity]
        assert await score_values(rig, closed.value.score_day) == expected_scores
        assert all(score.expires_at is None for score in await score_rows(rig, closed.value.score_day))
        db.run("SELECT msu_hub_private.retain_features(1000,clock_timestamp()+interval '90 days');")
        assert await score_values(rig, closed.value.score_day) == expected_scores
    finally:
        rig.worker.stop()
        await session.close()

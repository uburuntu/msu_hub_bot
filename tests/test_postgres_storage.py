"""Real SQL contracts; opt in with an empty disposable hub_test_* database."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import permutations
from pathlib import Path
from uuid import UUID

import pytest
from aiogram.types import Message, Update

from msu_hub_bot.storage.observations import archive_observation

SCHEMAS = sorted((Path(__file__).parents[1] / "dbschema/postgres").glob("*.sql"))
PRINCIPAL = "00000000-0000-0000-0000-000000000001"
STRANGER = "00000000-0000-0000-0000-000000000002"


def literal(value):
    return "'" + json.dumps(value, ensure_ascii=False).replace("'", "''") + "'::jsonb"


class Database:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql, *, principal=None, check=True):
        if principal:
            sql = f"SET ROLE authenticated; SET request.jwt.claim.sub = '{principal}';\n" + sql
        result = subprocess.run(
            ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-d", self.dsn],
            input=sql,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def value(self, sql, *, principal=None):
        return json.loads(self.run(sql, principal=principal).stdout)

    def rpc(self, name, args="", *, principal=PRINCIPAL):
        output = self.run(f"SELECT msu_hub_api.{name}_v1({args});", principal=principal).stdout.strip()
        if output in {"t", "f"}:
            return output == "t"
        return json.loads(output) if output else None


def namespace_snapshot(db, schema):
    assert schema in {"hub_private", "msu_hub_private"}
    tables = (
        "users",
        "chats",
        "chat_settings",
        "chat_users",
        "chat_topics",
        "updates",
        "messages",
        "directory",
        "vk_subscriptions",
        "mutation_journal",
        "principals",
        "schema_migrations",
    )
    rows = ",".join(
        f"'{table}',(SELECT jsonb_build_array(count(*), "
        "encode(sha256(convert_to(COALESCE(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),''),'UTF8')),'hex')) "
        f"FROM {schema}.{table} t" + (" WHERE version <> 3" if table == "schema_migrations" else "") + ")"
        for table in tables
    )
    snapshot = db.value(f"""
        WITH namespaces AS (
          SELECT * FROM pg_namespace WHERE nspname IN ('hub_private','hub_api','msu_hub_private','msu_hub_api')
        ), relations AS (SELECT * FROM pg_class WHERE relnamespace IN (SELECT oid FROM namespaces))
        SELECT jsonb_build_object(
          'rows',jsonb_build_object({rows}),
          'namespaces',(SELECT jsonb_agg(to_jsonb(n) ORDER BY oid) FROM namespaces n),
          'owner',(SELECT jsonb_agg(to_jsonb(r) ORDER BY oid) FROM pg_roles r WHERE rolname IN ('hub_owner','msu_hub_owner')),
          'relations',(SELECT jsonb_agg(jsonb_build_array(oid,relname,relkind,relfilenode,relowner,relacl,relrowsecurity,
                         relforcerowsecurity,relreplident,reloptions) ORDER BY oid) FROM relations),
          'types',(SELECT jsonb_agg(to_jsonb(t) ORDER BY oid) FROM pg_type t WHERE typnamespace IN (SELECT oid FROM namespaces)),
          'functions',(SELECT jsonb_agg((to_jsonb(p)-'proargdefaults') || jsonb_build_object(
                         'defaults',pg_get_expr(proargdefaults,0)) ORDER BY oid)
                         FROM pg_proc p WHERE pronamespace IN (SELECT oid FROM namespaces)),
          'constraints',(SELECT jsonb_agg(to_jsonb(c) ORDER BY oid) FROM pg_constraint c WHERE connamespace IN (SELECT oid FROM namespaces)),
          'defaults',(SELECT jsonb_agg(to_jsonb(d) ORDER BY oid) FROM pg_attrdef d WHERE adrelid IN (SELECT oid FROM relations)),
          'triggers',(SELECT jsonb_agg(to_jsonb(t) ORDER BY oid) FROM pg_trigger t WHERE tgrelid IN (SELECT oid FROM relations)),
          'privileges',(SELECT jsonb_agg(to_jsonb(a) ORDER BY oid) FROM pg_default_acl a WHERE defaclnamespace IN (SELECT oid FROM namespaces)),
          'sequence',(SELECT jsonb_build_array(last_value,is_called) FROM {schema}.mutation_journal_sequence_seq));
    """)
    # Compare identical catalog identities, allowing only the approved spelling change.
    return json.loads(
        json.dumps(snapshot)
        .replace("msu_hub_private", "hub_private")
        .replace("msu_hub_api", "hub_api")
        .replace("msu_hub_owner", "hub_owner")
    )


def exercise_namespace_migration(db, migration):
    """Exercise the predecessor once: owner roles are shared across databases."""
    db.run(f"""
        INSERT INTO hub_private.principals(auth_user_id,bot_id) VALUES ('{PRINCIPAL}',999);
        INSERT INTO hub_private.users(user_id,is_bot,first_name,metadata)
          VALUES(101,false,'Synthetic','{{"preserved":[1,null,"Привет"]}}');
        INSERT INTO hub_private.chats(chat_id,type,title,metadata) VALUES(-101,'supergroup','Synthetic','[1,2,3]');
        INSERT INTO hub_private.chat_settings(chat_id,settings) VALUES(-101,'{{"with_nsfw":false,"unknown":null}}');
        INSERT INTO hub_private.chat_users(chat_id,user_id,first_seen_at,last_seen_at,status)
          VALUES(-101,101,'2024-01-01','2024-02-01','member');
        INSERT INTO hub_private.chat_topics(chat_id,thread_id,first_seen_at,last_seen_at,title,is_closed)
          VALUES(-101,7,'2024-01-01','2024-02-01','Synthetic topic',false);
        INSERT INTO hub_private.updates(id,created,data,handled,bot_id,update_id)
          VALUES('00000000-0000-0000-0000-000000000077','2024-01-01','{{"message_refs":[77]}}',true,999,77);
        INSERT INTO hub_private.messages(bot_id,chat_id,message_id,sent_at,observed_at,data,source_update_id)
          VALUES(999,-101,77,'2024-01-01','2024-01-02','{{"text":"Synthetic body"}}','00000000-0000-0000-0000-000000000077');
        INSERT INTO hub_private.directory(chat_id,name,section) VALUES(-101,'Synthetic','other');
        INSERT INTO hub_private.vk_subscriptions(owner_id,chat_id,last_post_id) VALUES(-7,-101,42);
    """)
    original_function = db.run("SELECT pg_get_functiondef('hub_api.health_v1()'::regprocedure);").stdout
    cases = {
        "body_drift": (
            original_function.replace("'schema_version',1", "'schema_version',999"),
            original_function,
            "routine differs",
        ),
        "security_drift": (
            "ALTER FUNCTION hub_api.health_v1() SECURITY INVOKER;",
            "ALTER FUNCTION hub_api.health_v1() SECURITY DEFINER;",
            "routine differs",
        ),
        "search_path_drift": (
            "ALTER FUNCTION hub_api.health_v1() SET search_path='public';",
            "ALTER FUNCTION hub_api.health_v1() SET search_path='';",
            "routine differs",
        ),
        "owner_drift": (
            "ALTER FUNCTION hub_api.health_v1() OWNER TO CURRENT_USER;",
            "ALTER FUNCTION hub_api.health_v1() OWNER TO hub_owner;",
            "routine differs",
        ),
        "unexpected_function": (
            "CREATE FUNCTION hub_private.unreviewed() RETURNS int LANGUAGE sql AS 'SELECT 1';",
            "DROP FUNCTION hub_private.unreviewed();",
            "routine set differs",
        ),
        "private_collision": ("CREATE SCHEMA msu_hub_private;", "DROP SCHEMA msu_hub_private;", "target already exists"),
        "api_collision": ("CREATE SCHEMA msu_hub_api;", "DROP SCHEMA msu_hub_api;", "target already exists"),
        "owner_collision": ("CREATE ROLE msu_hub_owner NOLOGIN;", "DROP ROLE msu_hub_owner;", "target already exists"),
        "ledger_drift": (
            "INSERT INTO hub_private.schema_migrations(version) VALUES (99);",
            "DELETE FROM hub_private.schema_migrations WHERE version=99;",
            "requires application schema revision 2",
        ),
        "partial_ddl_rollback": (
            """CREATE FUNCTION public.reject_namespace_function() RETURNS event_trigger LANGUAGE plpgsql AS $$
               BEGIN IF to_regnamespace('msu_hub_private') IS NOT NULL AND to_regrole('msu_hub_owner') IS NOT NULL THEN
                 RAISE EXCEPTION 'synthetic failure after namespace and owner rename'; END IF; END; $$;
               CREATE EVENT TRIGGER reject_namespace_function ON ddl_command_end WHEN TAG IN ('CREATE FUNCTION')
                 EXECUTE FUNCTION public.reject_namespace_function();""",
            "DROP EVENT TRIGGER reject_namespace_function; DROP FUNCTION public.reject_namespace_function();",
            "synthetic failure after namespace and owner rename",
        ),
    }
    for name, (setup, cleanup, message) in cases.items():
        db.run(setup)
        before = namespace_snapshot(db, "hub_private")
        outcome = db.run(migration, check=False)
        assert outcome.returncode and message in outcome.stderr, (name, outcome.stderr)
        assert namespace_snapshot(db, "hub_private") == before, name
        db.run(cleanup)
    before = namespace_snapshot(db, "hub_private")
    db.run(migration)
    assert namespace_snapshot(db, "msu_hub_private") == before
    assert db.value("SELECT jsonb_agg(version ORDER BY version) FROM msu_hub_private.schema_migrations;") == [1, 2, 3]
    assert (
        db.run(
            "SELECT to_regnamespace('hub_private') IS NULL AND to_regnamespace('hub_api') IS NULL AND to_regrole('hub_owner') IS NULL;"
        ).stdout.strip()
        == "t"
    )
    assert db.rpc("health") == {"schema_version": 1, "bot_id": 999}
    assert db.rpc("get_chat", "-101")["metadata"] == [1, 2, 3]
    assert db.run("SELECT hub_api.health_v1();", principal=PRINCIPAL, check=False).returncode
    return {"failure_cases": set(cases), "populated_identity_preserved": True}


def exercise_reaction_migration(db, migration):
    """Keep the populated preceding schema compatible, including failed DDL."""
    db.run("INSERT INTO msu_hub_private.schema_migrations(version) VALUES(99);")
    rejected = db.run(migration, check=False)
    assert rejected.returncode and "requires schema revision 3" in rejected.stderr
    db.run("DELETE FROM msu_hub_private.schema_migrations WHERE version=99;")
    before = namespace_snapshot(db, "msu_hub_private")
    interrupted = migration.replace("INSERT INTO msu_hub_private.schema_migrations(version) VALUES(4);", "SELECT 1/0;")
    assert db.run(interrupted, check=False).returncode
    assert namespace_snapshot(db, "msu_hub_private") == before
    db.run(migration)
    after = namespace_snapshot(db, "msu_hub_private")
    assert {key: value for key, value in before["rows"].items() if key != "schema_migrations"} == {
        key: value for key, value in after["rows"].items() if key != "schema_migrations"
    }
    after_relations = {row[0]: row for row in after["relations"]}
    assert all(after_relations.get(row[0]) == row for row in before["relations"])
    assert db.rpc("health") == {"schema_version": 1, "bot_id": 999}
    assert db.rpc("get_chat", "-101")["metadata"] == [1, 2, 3]
    return True


@pytest.mark.parametrize(
    "case",
    [
        "body_drift",
        "security_drift",
        "search_path_drift",
        "owner_drift",
        "unexpected_function",
        "private_collision",
        "api_collision",
        "owner_collision",
        "ledger_drift",
        "partial_ddl_rollback",
    ],
)
def test_namespace_migration_failure_is_atomic(postgres, case):
    assert case in postgres.namespace_upgrade["failure_cases"]


def test_namespace_migration_preserves_populated_rows_and_object_identities(postgres):
    assert postgres.namespace_upgrade["populated_identity_preserved"]


def test_reaction_migration_is_additive_atomic_and_rejects_the_wrong_predecessor(postgres):
    assert postgres.reaction_upgrade


def archive(update_id=1, **extra):
    stamp = datetime.now(UTC).isoformat()
    return {
        "id": str(UUID(int=update_id)),
        "update_id": update_id,
        "received_at": stamp,
        "kind": "message",
        "handled": True,
        "data": {"update_id": update_id},
        "users": [{"user_id": 101, "is_bot": False, "first_name": "Друг", "observed_at": stamp}],
        "chats": [{"chat_id": -101, "type": "supergroup", "title": "Друзья", "observed_at": stamp}],
        **extra,
    }


def migration_target(db, directory):
    from urllib.parse import parse_qs, unquote, urlsplit

    from tools.migrate_storage import Postgres

    parsed = urlsplit(db.dsn)
    query = parse_qs(parsed.query)
    database = db.run("SELECT current_database();").stdout.strip()
    cluster = db.run("SELECT system_identifier FROM pg_control_system();").stdout.strip()
    connection = {
        "PGDATABASE": database,
        "PGHOST": parsed.hostname or query.get("host", [""])[0],
        "PGPORT": str(parsed.port or query.get("port", [5432])[0]),
    }
    if parsed.username:
        connection["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        connection["PGPASSWORD"] = unquote(parsed.password)
    return Postgres({"connection": connection, "expected_database": database, "expected_system_identifier": cluster}, directory, 999)


def test_administrative_copy_import_preserves_exact_values_and_resumes(db, tmp_path):
    from test_migrate_storage import make_export

    from tools.migrate_storage import decode, import_data, reconcile

    directory = tmp_path / "private-export"
    manifest = make_export(directory)
    target = migration_target(db, directory)
    import_data(target, directory, manifest, 1)
    import_data(target, directory, manifest, 1, replay=True)
    assert reconcile(target, directory, manifest)["exact"]
    assert db.value("SELECT count(*) FROM msu_hub_private.updates;") == 2
    assert db.value("SELECT data FROM msu_hub_private.updates ORDER BY id LIMIT 1;") is None
    assert decode(db.run("SELECT settings FROM msu_hub_private.chat_settings;").stdout)["future"]["unknown"][0] == decode(
        "1.00000000000000001"
    )
    assert db.run("SELECT min(first_seen_at)=min(created) FROM msu_hub_private.users;").stdout.strip() == "t"


def test_administrative_copy_failure_is_atomic_and_does_not_remove_existing_data(db, tmp_path):
    from test_migrate_storage import make_export, source_records

    from tools.migrate_storage import MigrationError, batch_script

    directory = tmp_path / "private-export"
    manifest = make_export(directory)
    target = migration_target(db, directory)
    db.run("INSERT INTO msu_hub_private.users(user_id,is_bot,first_name) VALUES(17,false,'Preserved');")
    records = source_records()["users"]
    records.append({**records[0], "id": "00000000-0000-0000-0000-000000000088"})
    with pytest.raises(MigrationError, match="target_operation_failed"):
        target.run(batch_script("users", records, manifest))
    assert db.value("SELECT count(*) FROM msu_hub_private.users;") == 1
    assert db.value("SELECT to_jsonb(first_name) FROM msu_hub_private.users;") == "Preserved"


def test_administrative_copy_json_null_metadata_survives(db, tmp_path):
    from test_migrate_storage import make_export, source_records

    from tools.migrate_storage import batch_script

    directory = tmp_path / "private-export"
    manifest = make_export(directory)
    target = migration_target(db, directory)
    records = source_records()["users"]
    records[0]["metadata"] = None
    target.run(batch_script("users", records, manifest))
    assert db.run("SELECT metadata='null'::jsonb AND metadata IS NOT NULL FROM msu_hub_private.users;").stdout.strip() == "t"


def test_administrative_normalization_is_replayable_and_hashes_winning_bodies(db, tmp_path):
    from test_migrate_storage import AS_OF, make_export, message_records

    from tools.migrate_storage import (
        MigrationError,
        audit_messages,
        import_data,
        normalize,
        prepare_normalization,
        reconcile,
        retention_report,
    )

    directory = tmp_path / "private-export"
    manifest = make_export(directory, message_records())
    target = migration_target(db, directory)
    import_data(target, directory, manifest, 1)
    assert reconcile(target, directory, manifest)["exact"]
    prepared = prepare_normalization(directory, manifest, AS_OF)
    assert prepared["counts"]["source_rows"] == 3 and not prepared["rejections"]
    normalize(target, directory, manifest, AS_OF, 1)
    normalize(target, directory, manifest, AS_OF, 1)
    assert reconcile(target, directory, manifest, normalized=True, as_of=AS_OF)["validated"]
    assert audit_messages(target, directory, manifest, AS_OF) == {
        "expected": 2,
        "actual": 2,
        "missing": 0,
        "extra": 0,
        "key_version_body_mismatches": 0,
        "body_sha256_mismatches": 0,
    }
    assert db.value("SELECT count(*) FROM msu_hub_private.updates;") == 3
    assert db.value("SELECT count(*) FROM msu_hub_private.updates WHERE data::text LIKE '%body%';") == 0
    assert db.value("SELECT to_jsonb(data->>'text') FROM msu_hub_private.messages WHERE message_id=42;") == "edited-parent-body"
    assert retention_report(target, AS_OF) == {
        "as_of": "2026-09-17T00:00:00.000000Z",
        "expired_updates": 1,
        "retained_updates": 2,
        "expired_messages": 0,
        "retained_messages": 2,
    }
    with pytest.raises(MigrationError, match="new_export"):
        import_data(target, directory, manifest, 1, replay=True)
    db.run("UPDATE msu_hub_private.messages SET data=jsonb_set(data,'{opaque}','0.12345678901234567890123456788') WHERE message_id=42;")
    assert audit_messages(target, directory, manifest, AS_OF)["body_sha256_mismatches"] == 1


def test_retained_window_migration_requires_exact_receipts_and_exposes_expired_target_extras(db, tmp_path):
    from test_migrate_storage import AS_OF, make_export, message_records

    from tools.migrate_storage import (
        MigrationError,
        audit_messages,
        batch_script,
        import_data,
        normalize,
        prepare_normalization,
        reconcile,
    )

    directory = tmp_path / "retained-export"
    records = message_records()
    manifest = make_export(directory, records, updates_since="2026-08-18T00:00:00Z")
    target = migration_target(db, directory)
    import_data(target, directory, manifest, 1)
    assert db.value("SELECT count(*) FROM msu_hub_private.updates;") == 2
    report = reconcile(target, directory, manifest)
    assert report["exact"] and report["validated"]
    assert report["selection"]["source_total"] == 3
    assert report["selection"]["selected"] == 2 and report["selection"]["excluded"] == 1
    assert db.value("SELECT count(*) FROM msu_hub_private.users WHERE created < '2026-08-18';") == 1

    # An expired receipt left by a broader import cannot disappear from parity.
    target.run(batch_script("updates", [records["updates"][-1]], manifest))
    report = reconcile(target, directory, manifest)
    assert report["tables"]["updates"]["extra"] == 1 and not report["validated"]
    prepared = prepare_normalization(directory, manifest, AS_OF)
    assert prepared["counts"]["source_rows"] == 2 and not prepared["rejections"]
    with pytest.raises(MigrationError, match="verified_raw_parity_required"):
        normalize(target, directory, manifest, AS_OF, 1)

    assert db.value(f"SELECT msu_hub_private.retain_messages(100,'{AS_OF}');")["updates"] == 1
    assert reconcile(target, directory, manifest)["exact"]
    normalize(target, directory, manifest, AS_OF, 1)
    assert reconcile(target, directory, manifest, normalized=True, as_of=AS_OF)["validated"]
    audited = audit_messages(target, directory, manifest, AS_OF)
    assert audited["expected"] == audited["actual"] == 2
    assert all(audited[key] == 0 for key in ("missing", "extra", "key_version_body_mismatches", "body_sha256_mismatches"))

    target.run(batch_script("updates", [records["updates"][-1]], manifest))
    report = reconcile(target, directory, manifest, normalized=True, retained_only=True, as_of=AS_OF)
    assert report["tables"]["updates"]["extra"] == 1 and not report["validated"]
    assert db.value("SELECT count(*) FROM msu_hub_private.users;") == 1


def test_administrative_normalization_failure_keeps_receipts_and_observations_atomic(db, tmp_path):
    from test_migrate_storage import AS_OF, make_export, message_records

    from tools.migrate_storage import MigrationError, import_data, normalization_script, transform_update

    directory = tmp_path / "private-export"
    records = message_records()
    manifest = make_export(directory, records)
    target = migration_target(db, directory)
    import_data(target, directory, manifest, 1)
    good = transform_update(records["updates"][0], AS_OF)
    bad = {**good, "id": "00000000-0000-0000-0000-000000000099"}
    with pytest.raises(MigrationError, match="target_operation_failed"):
        target.run(normalization_script([good, bad], 999, AS_OF))
    assert db.value("SELECT count(*) FROM msu_hub_private.messages;") == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.updates WHERE data::text LIKE '%body%';") == 3


def test_normalized_parity_accounts_only_observed_extra_entities(db, tmp_path):
    from test_migrate_storage import AS_OF, make_export, message_records

    from tools.migrate_storage import import_data, normalize, prepare_normalization, reconcile

    directory = tmp_path / "private-export"
    records = message_records()
    records["updates"][0]["data"]["message"]["from"]["id"] = 98765
    manifest = make_export(directory, records)
    target = migration_target(db, directory)
    import_data(target, directory, manifest, 10)
    assert reconcile(target, directory, manifest)["exact"]
    prepare_normalization(directory, manifest, AS_OF)
    normalize(target, directory, manifest, AS_OF, 10)
    report = reconcile(target, directory, manifest, normalized=True, as_of=AS_OF)
    assert not report["exact"] and report["source_parity"] and report["validated"]
    assert report["derived_entities"] == {
        "derived_users": 1,
        "derived_chats": 0,
        "unexplained_extra_entities": 0,
        "missing_observed_entities": 0,
    }
    db.run("INSERT INTO msu_hub_private.users(user_id,is_bot,first_name) VALUES(87654,false,'Unexplained');")
    report = reconcile(target, directory, manifest, normalized=True, as_of=AS_OF)
    assert not report["validated"] and report["derived_entities"]["unexplained_extra_entities"] == 1


def test_private_observation_helper_reuses_a_receipt_and_fixed_retention_instant(db):
    as_of = datetime(2024, 6, 1, tzinfo=UTC)
    stamp = as_of.isoformat()
    receipt = str(UUID(int=71))
    payload = archive(
        71,
        messages=[
            {
                "chat_id": -101,
                "message_id": 10,
                "sent_at": (as_of - timedelta(days=1)).isoformat(),
                "data": {"text": "One normalized body"},
            }
        ],
    )
    db.run(f"""
        INSERT INTO msu_hub_private.updates(id,created,data,handled,bot_id,is_legacy)
        VALUES ('{receipt}','{stamp}','{{"original":true}}',false,999,true);
    """)
    args = f"{literal(payload)},999,'{receipt}','{stamp}','{stamp}'"
    for _ in range(2):
        db.run(f"SELECT msu_hub_private.observe_archive({args});")
    assert db.value("SELECT count(*) FROM msu_hub_private.updates;") == 1
    assert db.value("SELECT data FROM msu_hub_private.updates;") == {"original": True}
    assert db.value("SELECT count(*) FROM msu_hub_private.messages;") == 1
    assert db.value("SELECT to_jsonb(source_update_id) FROM msu_hub_private.messages;") == receipt
    assert db.value("SELECT jsonb_agg(version ORDER BY version) FROM msu_hub_private.schema_migrations;") == [1, 2, 3, 4, 5]
    assert db.rpc("health") == {"schema_version": 1, "bot_id": 999}
    assert db.run(f"SELECT msu_hub_private.observe_archive({args});", principal=PRINCIPAL, check=False).returncode
    assert db.run(f"SET ROLE anon; SELECT msu_hub_private.observe_archive({args});", check=False).returncode


def test_principal_gate_covers_every_api_and_private_tables(db):
    arguments = {
        "health": "",
        "ensure_chat": "'{}'",
        "get_chat": "1",
        "load_settings": "'{}'",
        "patch_settings": "1,'{}'",
        "statistics": "now()",
        "list_directory": "",
        "get_directory": "1",
        "create_directory": "'{}'",
        "patch_directory": "1,'{}'",
        "delete_directory": "1",
        "list_vk_subscriptions": "",
        "upsert_vk_subscription": "1,1,'{}'",
        "advance_vk_cursor": "1,1,1",
        "archive_update": "'{}'",
        "reaction_scoreboard": "1,30,10",
    }
    for name, args in arguments.items():
        result = db.run(f"SELECT msu_hub_api.{name}_v1({args});", principal=STRANGER, check=False)
        assert result.returncode and "not authorized" in result.stderr
    assert db.rpc("health") == {"schema_version": 1, "bot_id": 999}
    assert db.run("SELECT * FROM msu_hub_private.users;", principal=PRINCIPAL, check=False).returncode
    assert db.run("SET ROLE anon; SELECT msu_hub_api.health_v1();", check=False).returncode
    assert db.run("SELECT msu_hub_private.retain_messages();", principal=PRINCIPAL, check=False).returncode


def test_definer_owner_has_no_platform_administration_privileges(db):
    assert (
        db.value("""
        SELECT jsonb_build_array(rolcanlogin,rolinherit,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls)
        FROM pg_roles WHERE rolname='msu_hub_owner';
    """)
        == [False] * 7
    )
    assert (
        db.value("""
        SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname IN ('msu_hub_private','msu_hub_api')
        AND (p.proowner <> 'msu_hub_owner'::regrole OR NOT p.proconfig @> ARRAY['search_path=""']);
    """)
        == 0
    )
    assert (
        db.value("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='msu_hub_private' AND c.relkind IN ('r','S') AND c.relowner <> 'msu_hub_owner'::regrole;
    """)
        == 0
    )
    assert db.value("SELECT count(*) FROM pg_auth_members WHERE member='msu_hub_owner'::regrole;") == 0
    db.run("CREATE TABLE public.unrelated_private_marker(value text); REVOKE ALL ON public.unrelated_private_marker FROM PUBLIC;")
    assert db.run("SET ROLE msu_hub_owner; SELECT * FROM public.unrelated_private_marker;", check=False).returncode
    assert db.run("SET ROLE msu_hub_owner; ALTER ROLE authenticated SUPERUSER;", check=False).returncode
    assert db.run("SET SESSION AUTHORIZATION authenticated; SET ROLE msu_hub_owner;", check=False).returncode
    assert db.value("SELECT to_jsonb(has_function_privilege('msu_hub_owner','auth.uid()','EXECUTE'));") is True


@pytest.mark.parametrize("metadata", ["{}", None, [], {"unknown": [1, None], "settings": {"with_nsfw": True, "future": 7}}])
def test_settings_preserve_original_metadata_and_atomic_patch(db, metadata):
    db.run(f"INSERT INTO msu_hub_private.chats(chat_id,type,metadata) VALUES (-101,'group',{literal(metadata)});")
    expected = metadata.get("settings", {}) if isinstance(metadata, dict) else {}
    assert db.rpc("load_settings", literal({"chat_id": -101, "type": "group"})) == expected
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: db.rpc("patch_settings", f"-101,{literal({f'option_{i}': i})}"), range(8)))
    assert db.value("SELECT metadata FROM msu_hub_private.chats WHERE chat_id=-101;") == metadata
    actual = db.value("SELECT settings FROM msu_hub_private.chat_settings WHERE chat_id=-101;")
    assert actual == {**expected, **{f"option_{i}": i for i in range(8)}}


def test_archive_is_atomic_idempotent_and_preserves_legacy_duplicates(db):
    values = archive()
    db.rpc("archive_update", literal(values))
    db.rpc("archive_update", literal(values))
    db.run("""INSERT INTO msu_hub_private.updates(id,created,data,handled,bot_id,update_id,is_legacy)
        VALUES ('00000000-0000-0000-0000-000000000099','2019-01-02T03:04:05.123456Z','null',false,999,1,true),
        ('00000000-0000-0000-0000-000000000098','2019-01-02T03:04:05.123456Z','[]',false,999,1,true);""")
    assert db.value("SELECT count(*) FROM msu_hub_private.updates;") == 3
    assert db.value("SELECT count(*) FROM msu_hub_private.users;") == 1
    bad = archive(2, users=[{"user_id": 202, "is_bot": False, "first_name": "Inserted then rolled back"}, {"user_id": 303}])
    assert db.run(f"SELECT msu_hub_api.archive_update_v1({literal(bad)});", principal=PRINCIPAL, check=False).returncode
    assert db.value("SELECT count(*) FROM msu_hub_private.users;") == 1
    assert db.value("SELECT count(*) FROM msu_hub_private.updates WHERE update_id=2;") == 0
    assert db.value("SELECT to_jsonb(created) FROM msu_hub_private.updates WHERE id='00000000-0000-0000-0000-000000000099';").startswith(
        "2019-01-02T03:04:05.123456"
    )


def test_observations_preserve_sparse_fields_and_reject_stale_profiles(db):
    db.rpc(
        "ensure_chat",
        literal(
            {
                "chat_id": -101,
                "type": "group",
                "title": "Current",
                "username": "current",
                "observed_at": "2026-01-02T00:00:00Z",
                "profile": {"is_forum": True},
            }
        ),
    )
    db.rpc("ensure_chat", literal({"chat_id": -101, "type": "group", "title": "Stale", "observed_at": "2026-01-01T00:00:00Z"}))
    db.rpc("ensure_chat", literal({"chat_id": -101, "type": "group", "observed_at": "2026-01-03T00:00:00Z"}))
    current = db.rpc("get_chat", "-101")
    assert current["title"] == "Current" and current["username"] == "current"
    assert current["profile"] == {"is_forum": True}
    db.rpc("ensure_chat", literal({"chat_id": -101, "type": "group", "username": None, "observed_at": "2026-01-04T00:00:00Z"}))
    assert db.rpc("get_chat", "-101")["username"] is None


def test_preference_lookup_does_not_refresh_a_stale_callback_chat_snapshot(db):
    chat = {
        "chat_id": -101,
        "type": "group",
        "title": "Current title",
        "observed_at": "2026-01-02T00:00:00Z",
        "profile": {"title": "Current title"},
    }
    before = db.rpc("ensure_chat", literal(chat))
    db.rpc(
        "load_settings",
        literal(
            {**chat, "title": "Stale callback title", "observed_at": "2026-02-01T00:00:00Z", "profile": {"title": "Stale callback title"}}
        ),
    )
    assert db.rpc("get_chat", "-101") == before


def test_normalized_entities_and_message_versions(db):
    now = datetime.now(UTC)
    message = {
        "chat_id": -101,
        "message_id": 7,
        "sent_at": (now - timedelta(hours=2)).isoformat(),
        "edited_at": (now - timedelta(hours=1)).isoformat(),
        "data": {"text": "Edited"},
    }
    db.rpc(
        "archive_update",
        literal(
            archive(
                messages=[message],
                memberships=[{"chat_id": -101, "user_id": 101, "status": "administrator", "permissions": {"can_delete_messages": True}}],
                topics=[{"chat_id": -101, "thread_id": 5, "title": "Topic"}],
            )
        ),
    )
    stale = {**message, "edited_at": None, "data": {"text": "Old"}}
    db.rpc(
        "archive_update",
        literal(archive(2, messages=[stale], memberships=[{"chat_id": -101, "user_id": 101, "status": "left", "permissions": {}}])),
    )
    assert db.value("SELECT data FROM msu_hub_private.messages;") == {"text": "Edited"}
    assert db.value("SELECT permissions FROM msu_hub_private.chat_users;") == {}
    assert db.value("SELECT to_jsonb(status) FROM msu_hub_private.chat_users;") == "left"
    assert db.value("SELECT to_jsonb(title) FROM msu_hub_private.chat_topics;") == "Topic"
    old = {**message, "message_id": 8, "sent_at": (now - timedelta(days=31)).isoformat(), "edited_at": now.isoformat()}
    db.rpc("archive_update", literal(archive(3, messages=[old])))
    assert db.value("SELECT count(*) FROM msu_hub_private.messages;") == 1


def test_directory_vk_shapes_and_tombstone_journal(db):
    created = db.rpc("create_directory", literal({"chat_id": -909, "name": "Name"}))
    assert created["section"] == "other" and created["is_hidden"] is False
    assert db.rpc("get_directory", "-123") is None
    assert db.rpc("patch_directory", "-909,'{\"members\":17}'")["members"] == 17
    assert db.rpc("patch_directory", "-909,'{\"members\":null}'")["members"] is None
    assert db.rpc("delete_directory", "-909") is True
    assert (
        db.value("SELECT row_data->'id' FROM msu_hub_private.mutation_journal WHERE relation_name='directory' AND operation='DELETE';")
        == created["id"]
    )
    assert db.value("SELECT row_data FROM msu_hub_private.mutation_journal WHERE relation_name='directory' AND operation='INSERT';") == {
        "id": created["id"],
        "chat_id": -909,
    }
    sub = db.rpc("upsert_vk_subscription", '-5,-909,\'{"description":"Saved","last_post_id":9}\'')
    assert sub["with_header"] is True and sub["with_reposts"] is False
    assert db.rpc("upsert_vk_subscription", "-5,-909,'{}'")["last_post_id"] == 9
    db.rpc("advance_vk_cursor", "-5,-909,7")
    assert db.rpc("list_vk_subscriptions")[0]["last_post_id"] == 9
    assert db.rpc("upsert_vk_subscription", '-5,-909,\'{"description":null,"last_post_id":0}\'')["description"] is None
    assert db.rpc("list_vk_subscriptions")[0]["last_post_id"] == 0


def test_lists_exceed_postgrest_default_row_cap_as_one_json_value(db):
    db.run("INSERT INTO msu_hub_private.directory(chat_id,name,section) SELECT -n,'Synthetic','other' FROM generate_series(1,1101) n;")
    assert len(db.rpc("list_directory")) == 1101


def test_required_subscription_fields_cannot_silently_use_defaults_when_cleared(db):
    assert db.run(
        "SELECT msu_hub_api.upsert_vk_subscription_v1(-5,-101,'{\"last_post_id\":null}');", principal=PRINCIPAL, check=False
    ).returncode
    assert db.rpc("list_vk_subscriptions") == []


def test_retention_is_bounded_and_never_deletes_durable_entities(db):
    db.rpc("archive_update", literal(archive()))
    db.rpc("load_settings", literal({"chat_id": -101, "type": "supergroup"}))
    db.rpc("create_directory", literal({"chat_id": -101, "name": "Retained"}))
    db.rpc("upsert_vk_subscription", "-5,-101,'{}'")
    db.run("""
        INSERT INTO msu_hub_private.updates(created,data,bot_id,update_id,is_legacy)
        VALUES ('2026-01-01','{}',999,11,true),('2026-01-02','{}',999,12,true),('2026-01-03','{}',999,13,true);
        INSERT INTO msu_hub_private.messages(bot_id,chat_id,message_id,sent_at,edited_at,observed_at,data)
        VALUES (999,-101,11,'2026-01-01','2026-02-01','2026-02-01','{}'),
        (999,-101,12,'2026-01-02','2026-02-01','2026-02-01','{}'),
        (999,-101,13,'2026-01-03','2026-02-01','2026-02-01','{}');
    """)
    before = db.value(
        "SELECT jsonb_build_array((SELECT count(*) FROM msu_hub_private.users),(SELECT count(*) FROM msu_hub_private.chats),(SELECT count(*) FROM msu_hub_private.chat_settings),(SELECT count(*) FROM msu_hub_private.directory),(SELECT count(*) FROM msu_hub_private.vk_subscriptions),(SELECT count(*) FROM msu_hub_private.mutation_journal));"
    )
    first = db.value("SELECT msu_hub_private.retain_messages(1,'2026-02-01');")
    assert first["messages"] == first["updates"] == 1
    second = db.value("SELECT msu_hub_private.retain_messages(100,'2026-02-01');")
    assert second["messages"] == second["updates"] == 1
    third = db.value("SELECT msu_hub_private.retain_messages(100,'2026-02-01');")
    assert third["messages"] == third["updates"] == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.messages;") == 1
    after = db.value(
        "SELECT jsonb_build_array((SELECT count(*) FROM msu_hub_private.users),(SELECT count(*) FROM msu_hub_private.chats),(SELECT count(*) FROM msu_hub_private.chat_settings),(SELECT count(*) FROM msu_hub_private.directory),(SELECT count(*) FROM msu_hub_private.vk_subscriptions),(SELECT count(*) FROM msu_hub_private.mutation_journal));"
    )
    assert before == after
    assert db.value("SELECT count(*) FROM msu_hub_private.mutation_journal WHERE relation_name IN ('updates','messages');") == 0


def test_reply_does_not_extend_near_expiry_body_through_receipt_or_parent(db):
    now = datetime.now(UTC)
    older = Message.model_validate(
        {
            "message_id": 2,
            "date": now - timedelta(days=29),
            "chat": {"id": -101, "type": "supergroup"},
            "text": "EXPIRING_BODY_CANARY",
        }
    )
    parent = Message.model_validate(
        {
            "message_id": 3,
            "date": now,
            "chat": {"id": -101, "type": "supergroup"},
            "text": "CURRENT_BODY_CANARY",
            "reply_to_message": older,
        }
    )
    row = archive_observation(Update(update_id=1, message=parent), False, received_at=now)
    db.rpc("archive_update", literal(row.model_dump(mode="json")))
    assert db.value("SELECT count(*) FROM msu_hub_private.messages;") == 2
    assert db.value("SELECT count(*) FROM msu_hub_private.updates WHERE data::text LIKE '%EXPIRING_BODY_CANARY%';") == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.messages WHERE message_id=3 AND data::text LIKE '%EXPIRING_BODY_CANARY%';") == 0
    later = (now + timedelta(days=1)).isoformat()
    result = db.value(f"SELECT msu_hub_private.retain_messages(100,'{later}');")
    assert result["messages"] == 1 and result["updates"] == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.messages WHERE data::text LIKE '%EXPIRING_BODY_CANARY%';") == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.messages WHERE data::text LIKE '%CURRENT_BODY_CANARY%';") == 1


@pytest.mark.parametrize("batch", ["NULL", "0", "10001"])
def test_retention_rejects_unbounded_or_invalid_batch(db, batch):
    assert db.run(f"SELECT msu_hub_private.retain_messages({batch});", check=False).returncode


def reaction(
    update_id, *, actor=202, message=42, chat=-101, keys=("e:❤",), counts=None, stamp=None, actor_chat=False, previous_active=False
):
    event_at = stamp or datetime.now(UTC) - timedelta(seconds=10)
    return archive(
        update_id,
        kind="message_reaction" if counts is None else "message_reaction_count",
        users=[
            {"user_id": identifier, "is_bot": identifier == 404, "first_name": f"Друг {identifier}"} for identifier in (101, 202, 303, 404)
        ],
        chats=[{"chat_id": chat, "type": "supergroup", "title": "Друзья"}],
        reaction={
            "kind": "actor" if counts is None else "counts",
            "chat_id": chat,
            "message_id": message,
            "event_at": event_at.isoformat(),
            "user_id": actor if counts is None and not actor_chat else None,
            "actor_chat_id": actor if counts is None and actor_chat else None,
            "previous_active": previous_active if counts is None else None,
            "reactions": [
                {"key": key, "count": count} for key, count in (counts.items() if counts is not None else ((k, 1) for k in keys))
            ],
        },
    )


def reaction_message(db, *, message=42, author=101, chat=-101, sender_chat=None, thread=7, stamp=None, business=""):
    stamp = (stamp or datetime.now(UTC) - timedelta(hours=1)).isoformat()
    db.rpc(
        "archive_update",
        literal(
            archive(
                10000 + message,
                messages=[
                    {
                        "chat_id": chat,
                        "message_id": message,
                        "sent_at": stamp,
                        "sender_user_id": author,
                        "sender_chat_id": sender_chat,
                        "thread_id": thread,
                        "business_connection_id": business,
                        "data": {"text": "PRIVATE_MESSAGE_BODY_CANARY"},
                    }
                ],
            )
        ),
    )


def test_reaction_scores_count_people_not_emoji_and_exclude_self_and_bots(db):
    reaction_message(db)
    for identifier, actor, keys in (
        (1, 202, ("e:❤", "e:🔥", "c:987654321012345678")),
        (2, 303, ("e:❤",)),
        (3, 101, ("e:🔥",)),
        (4, 404, ("e:🔥",)),
    ):
        db.rpc("archive_update", literal(reaction(identifier, actor=actor, keys=keys)))
    result = db.rpc("reaction_scoreboard", "-101,30,10")
    assert result["summary"] == {
        "points": 2,
        "reactions": 4,
        "givers": 2,
        "getters": 1,
        "messages": 1,
        "anonymous": 0,
        "paid": 0,
        "unattributed": 0,
        "channel_reactions": 0,
    }
    assert result["getters"][0] == {
        "user_id": 101,
        "first_name": "Друг 101",
        "last_name": None,
        "username": None,
        "score": 2,
        "people": 2,
        "messages": 1,
    }
    assert [row["user_id"] for row in result["givers"]] == [202, 303]
    assert result["emoji"][0] == {"key": "e:❤", "count": 2}
    assert result["posts"] == [{"message_id": 42, "thread_id": 7, "author_id": 101, "score": 2, "people": 2}]
    assert "PRIVATE_MESSAGE_BODY_CANARY" not in json.dumps(result)


def test_reaction_switch_removal_replay_and_equal_second_ordering(db):
    reaction_message(db)
    stamp = datetime.now(UTC) - timedelta(hours=2)
    initial = reaction(10, stamp=stamp)
    db.rpc("archive_update", literal(initial))
    initial_score_at = db.value("SELECT to_jsonb(score_at) FROM msu_hub_private.reaction_actors;")
    db.rpc("archive_update", literal(initial))
    switched = reaction(12, stamp=stamp, keys=("e:🔥", "e:👍"), previous_active=True)
    db.rpc("archive_update", literal(switched))
    db.rpc("archive_update", literal(reaction(9, stamp=stamp, keys=(), previous_active=True)))
    db.rpc("archive_update", literal(reaction(999, stamp=stamp - timedelta(seconds=1), keys=())))
    assert db.value("SELECT to_jsonb(score_at) FROM msu_hub_private.reaction_actors;") == initial_score_at
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 1
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["reactions"] == 2
    db.rpc("archive_update", literal(reaction(13, stamp=stamp + timedelta(seconds=1), keys=())))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.reaction_actors WHERE cleared_at > score_at AND reactions='[]';") == 1
    db.rpc("archive_update", literal(reaction(14, stamp=stamp, keys=("e:❤",))))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 0
    # Date is authoritative even if Telegram restarts its update ID sequence.
    db.rpc("archive_update", literal(reaction(1, stamp=stamp + timedelta(seconds=2))))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 1
    assert db.value("SELECT to_jsonb(score_at > '" + initial_score_at + "'::timestamptz) FROM msu_hub_private.reaction_actors;") is True


def test_reaction_unknown_authors_late_message_and_sender_chat_are_honest(db):
    db.rpc("archive_update", literal(reaction(1)))
    result = db.rpc("reaction_scoreboard", "-101")
    assert result["summary"]["points"] == result["summary"]["unattributed"] == 1
    assert result["givers"][0]["people"] == 0 and not result["getters"]
    assert result["posts"][0]["author_id"] is None
    reaction_message(db)
    assert db.rpc("reaction_scoreboard", "-101")["getters"][0]["user_id"] == 101
    # A compatibility from_user never identifies the person behind a sender_chat.
    reaction_message(db, message=43, author=202, sender_chat=-800)
    db.rpc("archive_update", literal(reaction(2, message=43)))
    result = db.rpc("reaction_scoreboard", "-101")
    assert result["summary"]["points"] == 2 and result["summary"]["unattributed"] == 1
    assert len(result["getters"]) == 1 and result["getters"][0]["score"] == 1
    assert next(post for post in result["posts"] if post["message_id"] == 43)["author_id"] is None
    reaction_message(db, message=44, business="separate-business-connection")
    db.rpc("archive_update", literal(reaction(3, message=44)))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["unattributed"] == 2


def test_reaction_anonymous_absolute_snapshots_paid_and_mode_changes(db):
    reaction_message(db)
    stamp = datetime.now(UTC) - timedelta(hours=1)
    db.rpc("archive_update", literal(reaction(1, stamp=stamp)))
    db.rpc("archive_update", literal(reaction(2, stamp=stamp, actor=303)))
    counts = reaction(3, counts={"e:❤": 30, "c:123": 2, "paid": 5}, stamp=stamp + timedelta(seconds=1))
    db.rpc("archive_update", literal(counts))
    db.rpc("archive_update", literal(counts))
    result = db.rpc("reaction_scoreboard", "-101")
    assert result["summary"]["points"] == 0
    assert result["summary"]["anonymous"] == 32 and result["summary"]["paid"] == 5
    assert not result["getters"] and not result["givers"]
    db.rpc("archive_update", literal(reaction(4, counts={"e:❤": 3}, stamp=stamp)))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["anonymous"] == 32
    # Identifiable mode resumes only with freshly observed actor snapshots.
    db.rpc("archive_update", literal(reaction(5, stamp=stamp + timedelta(seconds=2))))
    result = db.rpc("reaction_scoreboard", "-101")
    assert result["summary"]["points"] == 1
    assert result["summary"]["anonymous"] == result["summary"]["paid"] == 0
    db.rpc("archive_update", literal(reaction(6, actor=-500, actor_chat=True, message=43, stamp=stamp)))
    result = db.rpc("reaction_scoreboard", "-101")
    assert result["summary"]["points"] == 1 and result["summary"]["channel_reactions"] == 1
    # An empty absolute snapshot clears counts without losing its order watermark.
    db.rpc("archive_update", literal(reaction(7, counts={}, stamp=stamp + timedelta(seconds=3))))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 0


def test_reaction_windows_use_the_uninterrupted_point_time_and_hide_expired_rows(db):
    reaction_message(db)
    stamp = datetime.now(UTC)
    db.rpc("archive_update", literal(reaction(1, stamp=stamp - timedelta(days=8))))
    db.rpc("archive_update", literal(reaction(2, stamp=stamp - timedelta(days=2), keys=("e:🔥",), previous_active=True)))
    assert db.rpc("reaction_scoreboard", "-101,30,10")["summary"]["points"] == 1
    assert db.rpc("reaction_scoreboard", "-101,7,10")["summary"]["points"] == 0
    db.rpc("archive_update", literal(reaction(3, stamp=stamp - timedelta(hours=2), keys=())))
    db.rpc("archive_update", literal(reaction(4, stamp=stamp - timedelta(hours=1))))
    assert db.rpc("reaction_scoreboard", "-101,1,10")["summary"]["points"] == 1
    db.rpc("archive_update", literal(reaction(5, message=43, stamp=stamp - timedelta(days=31))))
    assert db.value("SELECT count(*) FROM msu_hub_private.reaction_actors;") == 1
    # Eligibility does not wait for scheduled physical deletion.
    db.run("""UPDATE msu_hub_private.reaction_actors SET score_at=now()-interval '32 days',
        cleared_at=now()-interval '33 days',event_at=now()-interval '31 days';""")
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.reaction_actors;") == 1


def test_reaction_retention_batches_are_independent_and_preserve_tombstones_and_durable_rows(db):
    stamp = datetime.now(UTC) - timedelta(hours=1)
    for identifier in range(1, 4):
        db.rpc("archive_update", literal(reaction(identifier, message=identifier, stamp=stamp, keys=() if identifier == 3 else ("e:❤",))))
        db.rpc("archive_update", literal(reaction(identifier + 10, message=identifier, counts={"e:🔥": identifier}, stamp=stamp)))
    db.run("""
        UPDATE msu_hub_private.reaction_actors SET score_at=now()-interval '32 days',event_at=now()-interval '31 days' WHERE message_id<3;
        UPDATE msu_hub_private.reaction_counts SET event_at=now()-interval '31 days' WHERE message_id<3;
    """)
    durable = db.value(
        "SELECT jsonb_build_array((SELECT count(*) FROM msu_hub_private.users),(SELECT count(*) FROM msu_hub_private.mutation_journal));"
    )
    for expected in (1, 1, 0):
        result = db.value("SELECT msu_hub_private.retain_messages(1);")
        assert result["reaction_actors"] == result["reaction_counts"] == expected
    assert db.value("SELECT count(*) FROM msu_hub_private.reaction_actors WHERE score_at IS NULL;") == 1
    assert (
        db.value(
            "SELECT jsonb_build_array((SELECT count(*) FROM msu_hub_private.users),(SELECT count(*) FROM msu_hub_private.mutation_journal));"
        )
        == durable
    )
    assert db.value("SELECT count(*) FROM msu_hub_private.mutation_journal WHERE relation_name LIKE 'reaction%';") == 0


def test_reaction_chat_and_bot_scope_and_disabled_principal(db):
    stamp = datetime.now(UTC) - timedelta(hours=1)
    db.rpc("archive_update", literal(reaction(1, stamp=stamp)))
    db.rpc("archive_update", literal(reaction(2, chat=-102, stamp=stamp)))
    db.run(f"INSERT INTO msu_hub_private.principals(auth_user_id,bot_id) VALUES('{STRANGER}',1000);")
    assert db.rpc("reaction_scoreboard", "-101", principal=STRANGER)["summary"]["points"] == 0
    second = reaction(1, stamp=stamp)
    second["id"] = str(UUID(int=200000))
    db.rpc("archive_update", literal(second), principal=STRANGER)
    assert db.rpc("reaction_scoreboard", "-101", principal=STRANGER)["summary"]["points"] == 1
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 1
    assert db.rpc("reaction_scoreboard", "-102")["summary"]["points"] == 1
    assert db.rpc("reaction_scoreboard", "-103")["summary"]["points"] == 0
    for table in ("reaction_actors", "reaction_counts"):
        assert db.run(f"SELECT * FROM msu_hub_private.{table};", principal=PRINCIPAL, check=False).returncode
    assert db.run("SELECT msu_hub_private.observe_reaction('{}',999,1);", principal=PRINCIPAL, check=False).returncode
    db.run(f"UPDATE msu_hub_private.principals SET enabled=false WHERE auth_user_id='{PRINCIPAL}';")
    assert db.run("SELECT msu_hub_api.reaction_scoreboard_v1(-101);", principal=PRINCIPAL, check=False).returncode


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "other"},
        {"kind": "counts", "user_id": 202},
        {"user_id": None},
        {"actor_chat_id": -600},
        {"chat_id": 0},
        {"message_id": 0},
        {"message_id": True},
        {"user_id": "202"},
        {"event_at": "infinity"},
        {"previous_active": None},
        {"previous_active": 1},
        {"kind": "counts", "user_id": None, "previous_active": False},
        {"reactions": [{"key": "e:❤", "count": 0}]},
        {"reactions": [{"key": "e:❤", "count": 2}]},
        {"reactions": [{"key": "e:❤", "count": True}]},
        {"reactions": [{"key": "c:", "count": 1}]},
        {"reactions": [{"key": "text", "count": 1}]},
        {"reactions": [{"key": "e:\n", "count": 1}]},
        {"reactions": [{"key": "e:❤", "count": 1}, {"key": "e:❤", "count": 1}]},
        {"reactions": [{"key": "c:" + "1" * 255, "count": 1}]},
        {"reactions": [{"key": "c:" + str(i), "count": 1} for i in range(257)]},
        {"unexpected": "private-canary"},
    ],
)
def test_reaction_invalid_snapshots_roll_back_the_whole_archive(db, changes):
    row = reaction(1)
    row["reaction"].update(changes)
    result = db.run(f"SELECT msu_hub_api.archive_update_v1({literal(row)});", principal=PRINCIPAL, check=False)
    assert result.returncode
    assert db.value("SELECT count(*) FROM msu_hub_private.updates;") == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.users;") == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.reaction_actors;") == 0


@pytest.mark.parametrize("arguments", ["-101,0,10", "-101,2,10", "-101,31,10", "-101,30,0", "-101,30,11", "0,30,10", "-101,NULL,10"])
def test_reaction_statistics_reject_invalid_scope_and_limits(db, arguments):
    assert db.run(f"SELECT msu_hub_api.reaction_scoreboard_v1({arguments});", principal=PRINCIPAL, check=False).returncode


def test_reaction_concurrent_updates_and_receipt_replay_choose_the_latest_snapshot(db):
    reaction_message(db)
    stamp = datetime.now(UTC) - timedelta(hours=1)
    rows = [reaction(i, stamp=stamp, keys=("e:❤",) if i < 12 else ()) for i in range(1, 13)]
    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(lambda row: db.rpc("archive_update", literal(row)), rows[::-1] + rows))
    assert db.value("SELECT count(*) FROM msu_hub_private.updates;") == 13
    assert db.value("SELECT to_jsonb(update_id) FROM msu_hub_private.reaction_actors;") == 12
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 0


@pytest.mark.parametrize("with_reentry", [False, True])
def test_reaction_transition_watermarks_are_independent_of_archive_completion_order(db, with_reentry):
    reaction_message(db)
    stamp = datetime.now(UTC)
    rows = [reaction(1, stamp=stamp - timedelta(days=8))]
    if with_reentry:
        rows.extend(
            [
                reaction(2, stamp=stamp - timedelta(hours=2), keys=(), previous_active=True),
                reaction(3, stamp=stamp - timedelta(hours=1), previous_active=False),
            ]
        )
    rows.append(reaction(4, stamp=stamp - timedelta(minutes=30), keys=("e:🔥", "e:👍"), previous_active=True))
    outcomes = []
    for ordered in permutations(rows):
        calls = "\n".join(f"SELECT msu_hub_api.archive_update_v1({literal(row)});" for row in ordered)
        result = db.value(f"""
            BEGIN;
            TRUNCATE msu_hub_private.updates,msu_hub_private.reaction_actors,msu_hub_private.reaction_counts;
            SET LOCAL request.jwt.claim.sub = '{PRINCIPAL}';
            {calls}
            SELECT jsonb_build_object(
                'week',msu_hub_api.reaction_scoreboard_v1(-101,7,10),
                'month',msu_hub_api.reaction_scoreboard_v1(-101,30,10),
                'state',(SELECT to_jsonb(r) FROM msu_hub_private.reaction_actors r));
            COMMIT;
        """)
        outcomes.append(result)
    assert all(outcome == outcomes[0] for outcome in outcomes)
    assert outcomes[0]["week"]["summary"]["points"] == int(with_reentry)
    assert outcomes[0]["month"]["summary"]["points"] == 1
    assert outcomes[0]["state"]["score_update_id"] == (3 if with_reentry else 1)
    assert outcomes[0]["state"]["update_id"] == 4
    assert outcomes[0]["state"]["reactions"] == [{"key": "e:👍", "count": 1}, {"key": "e:🔥", "count": 1}]


def test_reaction_unobserved_start_is_not_invented_and_stale_clear_cannot_cancel_newer_start(db):
    stamp = datetime.now(UTC) - timedelta(hours=2)
    db.rpc("archive_update", literal(reaction(4, stamp=stamp + timedelta(seconds=2), previous_active=True)))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 0
    # A late observed start repairs attribution without replacing the latest choices.
    db.rpc("archive_update", literal(reaction(3, stamp=stamp + timedelta(seconds=1))))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 1
    db.rpc("archive_update", literal(reaction(2, stamp=stamp, keys=(), previous_active=True)))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 1
    assert db.value("SELECT to_jsonb(update_id) FROM msu_hub_private.reaction_actors;") == 4


@pytest.mark.parametrize("counts", [{}, {"e:❤": 5}])
def test_reaction_anonymous_mode_fences_individual_continuity_until_an_observed_new_start(db, counts):
    stamp = datetime.now(UTC) - timedelta(hours=1)
    db.rpc("archive_update", literal(reaction(1, stamp=stamp)))
    db.rpc("archive_update", literal(reaction(2, stamp=stamp + timedelta(seconds=1), counts=counts)))
    db.rpc("archive_update", literal(reaction(4, stamp=stamp + timedelta(seconds=3), previous_active=True)))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 0
    db.rpc("archive_update", literal(reaction(3, stamp=stamp + timedelta(seconds=2))))
    assert db.rpc("reaction_scoreboard", "-101")["summary"]["points"] == 1
    assert db.value("SELECT to_jsonb(update_id) FROM msu_hub_private.reaction_actors;") == 4

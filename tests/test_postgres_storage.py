"""Real SQL contracts; opt in with an empty disposable hub_test_* database."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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


@pytest.fixture(scope="module")
def postgres():
    dsn = os.environ.get("HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set HUB_TEST_POSTGRES_DSN for the real PostgreSQL contract suite")
    if not shutil.which("psql"):
        pytest.fail("PostgreSQL contract tests require psql")
    db = Database(dsn)
    name = db.run("SELECT current_database();").stdout.strip()
    if not name.startswith("hub_test_"):
        pytest.fail("Refusing to initialize a database outside the hub_test_ namespace")
    if (
        db.run(
            "SELECT count(*) FROM pg_namespace WHERE nspname IN ('hub_private','hub_api','msu_hub_private','msu_hub_api','auth');"
        ).stdout.strip()
        != "0"
    ):
        pytest.fail("PostgreSQL contract tests require an empty disposable database")
    db.run("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon; END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated; END IF;
        END $$;
        CREATE SCHEMA auth;
        CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
        $$ SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
    """)
    for schema in SCHEMAS:
        if schema.name == "003_application_namespaces.sql":
            db.namespace_upgrade = exercise_namespace_migration(db, schema.read_text())
        else:
            db.run(schema.read_text())
    return db


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


@pytest.fixture
def db(postgres):
    # The module fixture refuses existing schemas before installing the test schema.
    postgres.run("""
        TRUNCATE msu_hub_private.chat_users,msu_hub_private.chat_topics,msu_hub_private.chat_settings,
            msu_hub_private.messages,msu_hub_private.updates,msu_hub_private.users,msu_hub_private.chats,
            msu_hub_private.directory,msu_hub_private.vk_subscriptions,msu_hub_private.mutation_journal,msu_hub_private.principals;
        INSERT INTO msu_hub_private.principals(auth_user_id,bot_id) VALUES ('00000000-0000-0000-0000-000000000001',999);
    """)
    return postgres


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
    assert db.value("SELECT jsonb_agg(version ORDER BY version) FROM msu_hub_private.schema_migrations;") == [1, 2, 3]
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

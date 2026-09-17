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

from common.db.observations import archive_observation

SCHEMA = Path(__file__).parents[1] / "dbschema/postgres/001_bot_storage.sql"
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
        output = self.run(f"SELECT hub_api.{name}_v1({args});", principal=principal).stdout.strip()
        if output in {"t", "f"}:
            return output == "t"
        return json.loads(output) if output else None


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
    if db.run("SELECT count(*) FROM pg_namespace WHERE nspname IN ('hub_private','hub_api','auth');").stdout.strip() != "0":
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
    db.run(SCHEMA.read_text())
    return db


@pytest.fixture
def db(postgres):
    # The module fixture refuses existing schemas before installing the test schema.
    postgres.run("""
        TRUNCATE hub_private.chat_users,hub_private.chat_topics,hub_private.chat_settings,
            hub_private.messages,hub_private.updates,hub_private.users,hub_private.chats,
            hub_private.directory,hub_private.vk_subscriptions,hub_private.mutation_journal,hub_private.principals;
        INSERT INTO hub_private.principals(auth_user_id,bot_id) VALUES ('00000000-0000-0000-0000-000000000001',999);
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
    assert db.value("SELECT count(*) FROM hub_private.updates;") == 2
    assert db.value("SELECT data FROM hub_private.updates ORDER BY id LIMIT 1;") is None
    assert decode(db.run("SELECT settings FROM hub_private.chat_settings;").stdout)["future"]["unknown"][0] == decode("1.00000000000000001")
    assert db.run("SELECT min(first_seen_at)=min(created) FROM hub_private.users;").stdout.strip() == "t"


def test_administrative_copy_failure_is_atomic_and_does_not_remove_existing_data(db, tmp_path):
    from test_migrate_storage import make_export, source_records

    from tools.migrate_storage import MigrationError, batch_script

    directory = tmp_path / "private-export"
    manifest = make_export(directory)
    target = migration_target(db, directory)
    db.run("INSERT INTO hub_private.users(user_id,is_bot,first_name) VALUES(17,false,'Preserved');")
    records = source_records()["users"]
    records.append({**records[0], "id": "00000000-0000-0000-0000-000000000088"})
    with pytest.raises(MigrationError, match="target_operation_failed"):
        target.run(batch_script("users", records, manifest))
    assert db.value("SELECT count(*) FROM hub_private.users;") == 1
    assert db.value("SELECT to_jsonb(first_name) FROM hub_private.users;") == "Preserved"


def test_administrative_copy_json_null_metadata_survives(db, tmp_path):
    from test_migrate_storage import make_export, source_records

    from tools.migrate_storage import batch_script

    directory = tmp_path / "private-export"
    manifest = make_export(directory)
    target = migration_target(db, directory)
    records = source_records()["users"]
    records[0]["metadata"] = None
    target.run(batch_script("users", records, manifest))
    assert db.run("SELECT metadata='null'::jsonb AND metadata IS NOT NULL FROM hub_private.users;").stdout.strip() == "t"


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
        result = db.run(f"SELECT hub_api.{name}_v1({args});", principal=STRANGER, check=False)
        assert result.returncode and "not authorized" in result.stderr
    assert db.rpc("health") == {"schema_version": 1, "bot_id": 999}
    assert db.run("SELECT * FROM hub_private.users;", principal=PRINCIPAL, check=False).returncode
    assert db.run("SET ROLE anon; SELECT hub_api.health_v1();", check=False).returncode
    assert db.run("SELECT hub_private.retain_messages();", principal=PRINCIPAL, check=False).returncode


def test_definer_owner_has_no_platform_administration_privileges(db):
    assert (
        db.value("""
        SELECT jsonb_build_array(rolcanlogin,rolinherit,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls)
        FROM pg_roles WHERE rolname='hub_owner';
    """)
        == [False] * 7
    )
    assert (
        db.value("""
        SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname IN ('hub_private','hub_api')
        AND (p.proowner <> 'hub_owner'::regrole OR NOT p.proconfig @> ARRAY['search_path=""']);
    """)
        == 0
    )
    assert (
        db.value("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='hub_private' AND c.relkind IN ('r','S') AND c.relowner <> 'hub_owner'::regrole;
    """)
        == 0
    )
    assert db.value("SELECT count(*) FROM pg_auth_members WHERE member='hub_owner'::regrole;") == 0
    db.run("CREATE TABLE public.unrelated_private_marker(value text); REVOKE ALL ON public.unrelated_private_marker FROM PUBLIC;")
    assert db.run("SET ROLE hub_owner; SELECT * FROM public.unrelated_private_marker;", check=False).returncode
    assert db.run("SET ROLE hub_owner; ALTER ROLE authenticated SUPERUSER;", check=False).returncode
    assert db.run("SET SESSION AUTHORIZATION authenticated; SET ROLE hub_owner;", check=False).returncode
    assert db.value("SELECT to_jsonb(has_function_privilege('hub_owner','auth.uid()','EXECUTE'));") is True


@pytest.mark.parametrize("metadata", ["{}", None, [], {"unknown": [1, None], "settings": {"with_nsfw": True, "future": 7}}])
def test_settings_preserve_original_metadata_and_atomic_patch(db, metadata):
    db.run(f"INSERT INTO hub_private.chats(chat_id,type,metadata) VALUES (-101,'group',{literal(metadata)});")
    expected = metadata.get("settings", {}) if isinstance(metadata, dict) else {}
    assert db.rpc("load_settings", literal({"chat_id": -101, "type": "group"})) == expected
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: db.rpc("patch_settings", f"-101,{literal({f'option_{i}': i})}"), range(8)))
    assert db.value("SELECT metadata FROM hub_private.chats WHERE chat_id=-101;") == metadata
    actual = db.value("SELECT settings FROM hub_private.chat_settings WHERE chat_id=-101;")
    assert actual == {**expected, **{f"option_{i}": i for i in range(8)}}


def test_archive_is_atomic_idempotent_and_preserves_legacy_duplicates(db):
    values = archive()
    db.rpc("archive_update", literal(values))
    db.rpc("archive_update", literal(values))
    db.run("""INSERT INTO hub_private.updates(id,created,data,handled,bot_id,update_id,is_legacy)
        VALUES ('00000000-0000-0000-0000-000000000099','2019-01-02T03:04:05.123456Z','null',false,999,1,true),
        ('00000000-0000-0000-0000-000000000098','2019-01-02T03:04:05.123456Z','[]',false,999,1,true);""")
    assert db.value("SELECT count(*) FROM hub_private.updates;") == 3
    assert db.value("SELECT count(*) FROM hub_private.users;") == 1
    bad = archive(2, users=[{"user_id": 202, "is_bot": False, "first_name": "Inserted then rolled back"}, {"user_id": 303}])
    assert db.run(f"SELECT hub_api.archive_update_v1({literal(bad)});", principal=PRINCIPAL, check=False).returncode
    assert db.value("SELECT count(*) FROM hub_private.users;") == 1
    assert db.value("SELECT count(*) FROM hub_private.updates WHERE update_id=2;") == 0
    assert db.value("SELECT to_jsonb(created) FROM hub_private.updates WHERE id='00000000-0000-0000-0000-000000000099';").startswith(
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
    assert db.value("SELECT data FROM hub_private.messages;") == {"text": "Edited"}
    assert db.value("SELECT permissions FROM hub_private.chat_users;") == {}
    assert db.value("SELECT to_jsonb(status) FROM hub_private.chat_users;") == "left"
    assert db.value("SELECT to_jsonb(title) FROM hub_private.chat_topics;") == "Topic"
    old = {**message, "message_id": 8, "sent_at": (now - timedelta(days=31)).isoformat(), "edited_at": now.isoformat()}
    db.rpc("archive_update", literal(archive(3, messages=[old])))
    assert db.value("SELECT count(*) FROM hub_private.messages;") == 1


def test_directory_vk_shapes_and_tombstone_journal(db):
    created = db.rpc("create_directory", literal({"chat_id": -909, "name": "Name"}))
    assert created["section"] == "other" and created["is_hidden"] is False
    assert db.rpc("get_directory", "-123") is None
    assert db.rpc("patch_directory", "-909,'{\"members\":17}'")["members"] == 17
    assert db.rpc("patch_directory", "-909,'{\"members\":null}'")["members"] is None
    assert db.rpc("delete_directory", "-909") is True
    assert (
        db.value("SELECT row_data->'id' FROM hub_private.mutation_journal WHERE relation_name='directory' AND operation='DELETE';")
        == created["id"]
    )
    assert db.value("SELECT row_data FROM hub_private.mutation_journal WHERE relation_name='directory' AND operation='INSERT';") == {
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
    db.run("INSERT INTO hub_private.directory(chat_id,name,section) SELECT -n,'Synthetic','other' FROM generate_series(1,1101) n;")
    assert len(db.rpc("list_directory")) == 1101


def test_required_subscription_fields_cannot_silently_use_defaults_when_cleared(db):
    assert db.run(
        "SELECT hub_api.upsert_vk_subscription_v1(-5,-101,'{\"last_post_id\":null}');", principal=PRINCIPAL, check=False
    ).returncode
    assert db.rpc("list_vk_subscriptions") == []


def test_retention_is_bounded_and_never_deletes_durable_entities(db):
    db.rpc("archive_update", literal(archive()))
    db.rpc("load_settings", literal({"chat_id": -101, "type": "supergroup"}))
    db.rpc("create_directory", literal({"chat_id": -101, "name": "Retained"}))
    db.rpc("upsert_vk_subscription", "-5,-101,'{}'")
    db.run("""
        INSERT INTO hub_private.updates(created,data,bot_id,update_id,is_legacy)
        VALUES ('2026-01-01','{}',999,11,true),('2026-01-02','{}',999,12,true),('2026-01-03','{}',999,13,true);
        INSERT INTO hub_private.messages(bot_id,chat_id,message_id,sent_at,edited_at,observed_at,data)
        VALUES (999,-101,11,'2026-01-01','2026-02-01','2026-02-01','{}'),
        (999,-101,12,'2026-01-02','2026-02-01','2026-02-01','{}'),
        (999,-101,13,'2026-01-03','2026-02-01','2026-02-01','{}');
    """)
    before = db.value(
        "SELECT jsonb_build_array((SELECT count(*) FROM hub_private.users),(SELECT count(*) FROM hub_private.chats),(SELECT count(*) FROM hub_private.chat_settings),(SELECT count(*) FROM hub_private.directory),(SELECT count(*) FROM hub_private.vk_subscriptions),(SELECT count(*) FROM hub_private.mutation_journal));"
    )
    first = db.value("SELECT hub_private.retain_messages(1,'2026-02-01');")
    assert first["messages"] == first["updates"] == 1
    second = db.value("SELECT hub_private.retain_messages(100,'2026-02-01');")
    assert second["messages"] == second["updates"] == 1
    third = db.value("SELECT hub_private.retain_messages(100,'2026-02-01');")
    assert third["messages"] == third["updates"] == 0
    assert db.value("SELECT count(*) FROM hub_private.messages;") == 1
    after = db.value(
        "SELECT jsonb_build_array((SELECT count(*) FROM hub_private.users),(SELECT count(*) FROM hub_private.chats),(SELECT count(*) FROM hub_private.chat_settings),(SELECT count(*) FROM hub_private.directory),(SELECT count(*) FROM hub_private.vk_subscriptions),(SELECT count(*) FROM hub_private.mutation_journal));"
    )
    assert before == after
    assert db.value("SELECT count(*) FROM hub_private.mutation_journal WHERE relation_name IN ('updates','messages');") == 0


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
    assert db.value("SELECT count(*) FROM hub_private.messages;") == 2
    assert db.value("SELECT count(*) FROM hub_private.updates WHERE data::text LIKE '%EXPIRING_BODY_CANARY%';") == 0
    assert db.value("SELECT count(*) FROM hub_private.messages WHERE message_id=3 AND data::text LIKE '%EXPIRING_BODY_CANARY%';") == 0
    later = (now + timedelta(days=1)).isoformat()
    result = db.value(f"SELECT hub_private.retain_messages(100,'{later}');")
    assert result["messages"] == 1 and result["updates"] == 0
    assert db.value("SELECT count(*) FROM hub_private.messages WHERE data::text LIKE '%EXPIRING_BODY_CANARY%';") == 0
    assert db.value("SELECT count(*) FROM hub_private.messages WHERE data::text LIKE '%CURRENT_BODY_CANARY%';") == 1


@pytest.mark.parametrize("batch", ["NULL", "0", "10001"])
def test_retention_rejects_unbounded_or_invalid_batch(db, batch):
    assert db.run(f"SELECT hub_private.retain_messages({batch});", check=False).returncode

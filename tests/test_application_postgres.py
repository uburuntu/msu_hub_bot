"""Stopped-writer consolidation and the resulting application document contract."""

import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID

import pytest
from test_feature_postgres import call, guard, transaction
from test_postgres_storage import PRINCIPAL, STRANGER, literal, namespace_snapshot

SCOPE = {"key": "global", "owner": "application"}
DESTINATIONS = "(feature, collection) IN (('settings','chats'),('ecosystem','chats'),('vk','subscriptions'))"


def document_repository(db, *, lose_response=False):
    from msu_hub_bot.storage.application import ApplicationDocuments
    from msu_hub_bot.storage.errors import RepositoryFailure, RepositoryUnavailable
    from msu_hub_bot.storage.features import FeatureStore
    from msu_hub_bot.storage.models import ChatRecord

    class Backend:
        def __init__(self):
            self.lost = False
            self.requests = []

        async def feature_request(self, operation, request):
            result = await asyncio.to_thread(call, db, operation, request)
            if operation == "commit":
                self.requests.append(request)
                if lose_response and not self.lost:
                    self.lost = True
                    raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
            return result

    async def lookup(chat_id):
        value = await asyncio.to_thread(db.rpc, "get_chat", str(chat_id))
        return ChatRecord.model_validate(value) if value is not None else None

    backend = Backend()
    return ApplicationDocuments(FeatureStore(backend), lookup), backend


async def verify_migrated_models(db):
    documents, _ = document_repository(db)
    observed = await documents.chat_lookup(-101)
    assert observed is not None
    settings = await documents.load_settings(observed)
    assert settings["auto_video_links"] is False and settings["auto_speech_recognition"] is True and settings["with_nsfw"] is False
    assert settings["future"]["null"] is None
    entries = await documents.list_directory()
    assert [entry.chat_id for entry in entries] == [-999, -101]
    entry = entries[-1]
    assert entry.id == UUID(int=17) and entry.created == datetime(2019, 4, 5, tzinfo=UTC)
    assert entry.members == 17 and entry.pinned_message_id == 31 and entry.username_alias is None
    subscriptions = await documents.list_vk_subscriptions()
    assert [(item.owner_id, item.chat_id) for item in subscriptions] == [(-5, -101), (5, -999)]
    item = subscriptions[0]
    assert item.id == UUID(int=19) and item.created == datetime(2019, 5, 6, tzinfo=UTC)
    assert item.last_post_id == 123 and item.with_reposts and not item.with_header and item.is_suspended and item.description is None


def rows_snapshot(db, tables):
    return {
        table: db.value(
            "SELECT jsonb_build_array(count(*),encode(sha256(convert_to("
            "COALESCE(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),''),'UTF8')),'hex')) "
            f"FROM msu_hub_private.{table} t;"
        )
        for table in tables
    }


def assert_locked_writer_rejects_migration(db, migration):
    process = subprocess.Popen(
        ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-d", db.dsn],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        process.stdin.write("BEGIN; LOCK TABLE msu_hub_private.chat_settings IN ROW EXCLUSIVE MODE; SELECT 'locked';\n")
        process.stdin.flush()
        assert process.stdout.readline().strip() == "locked"
        result = db.run(migration.replace("lock_timeout = '3s'", "lock_timeout = '100ms'"), check=False)
        assert result.returncode and "lock timeout" in result.stderr
    finally:
        process.communicate("ROLLBACK;\n", timeout=10)
        assert process.returncode == 0


def exercise_application_migrations(db, schemas):
    assert [schema.name[:3] for schema in schemas] == ["006", "007"]
    backfill, retire = [schema.read_text() for schema in schemas]
    db.run("""
        INSERT INTO msu_hub_private.users(id,created,user_id,is_bot,first_name,metadata)
        VALUES ('00000000-0000-0000-0000-000000000010','2019-01-02',10,false,'Preserved','{"unknown":null}');
        INSERT INTO msu_hub_private.chats(created,chat_id,type,title,metadata)
        VALUES ('2019-02-03',-101,'supergroup','Preserved','{"settings":{"with_nsfw":true}}'),
            ('2019-03-04',-202,'group','Unopened','{"settings":{"auto_video_links":false}}');
        INSERT INTO msu_hub_private.chat_settings(chat_id,settings,updated_at)
        VALUES (-101,'{"auto_video_links":false,"future":{"precision":1.00000000000000001,"null":null}}','2020-04-05');
        INSERT INTO msu_hub_private.directory(id,created,chat_id,name,section,is_hidden,username_alias,members,pinned_message_id)
        VALUES ('00000000-0000-0000-0000-000000000011','2019-04-05 UTC',-101,'Друзья','friends',false,null,17,31),
            ('00000000-0000-0000-0000-000000000012','2019-04-06 UTC',-999,'Independent','other',true,'alias',null,null);
        INSERT INTO msu_hub_private.vk_subscriptions
            (id,created,owner_id,chat_id,last_post_id,with_reposts,with_header,is_suspended,description)
        VALUES ('00000000-0000-0000-0000-000000000013','2019-05-06 UTC',-5,-101,123,true,false,true,null),
            ('00000000-0000-0000-0000-000000000014','2019-05-07 UTC',5,-999,0,false,true,false,'Сохранено');
        INSERT INTO msu_hub_private.chat_users(chat_id,user_id,first_seen_at,last_seen_at,status,permissions)
        VALUES (-101,10,'2020-01-01','2020-02-01','administrator','{"can_pin_messages":true}');
        INSERT INTO msu_hub_private.chat_topics(chat_id,thread_id,first_seen_at,last_seen_at,title)
        VALUES (-101,7,'2020-01-01','2020-02-01','Preserved topic');
        INSERT INTO msu_hub_private.messages(bot_id,chat_id,message_id,sent_at,observed_at,data)
        VALUES (999,-101,31,now(),now(),'{"text":"synthetic retained message"}');
        INSERT INTO msu_hub_private.updates(data,bot_id,update_id) VALUES ('{}',999,1);
        INSERT INTO msu_hub_private.feature_records(owner_id,feature,scope_key,collection,key,payload_version,payload)
        VALUES (999,'unrelated','chat:-101','records','keep',1,'{"untouched":true}');
        INSERT INTO msu_hub_private.feature_jobs(owner_id,feature,scope_key,key,kind,record_collection,record_key,run_at)
        VALUES (999,'unrelated','chat:-101','keep','keep','records','keep',now());
        INSERT INTO msu_hub_private.feature_operations(owner_id,feature,scope_key,operation_id,request_hash,result,expires_at)
        VALUES (999,'unrelated','chat:-101','keep',sha256('synthetic'::bytea),'{}',now()+interval '7 days');
    """)
    db.rpc("create_directory", literal({"chat_id": -303, "name": "Deleted before consolidation"}))
    db.rpc("delete_directory", "-303")
    durable = (
        "users",
        "chats",
        "chat_users",
        "chat_topics",
        "messages",
        "updates",
        "principals",
        "reaction_actors",
        "reaction_counts",
        "mutation_journal",
        "feature_jobs",
        "feature_operations",
    )
    unchanged = rows_snapshot(db, durable)
    rejected_cases = set()

    def reject(name, script, message):
        before = namespace_snapshot(db, "msu_hub_private")
        feature_rows = rows_snapshot(db, ("feature_records", "feature_jobs", "feature_operations"))
        result = db.run(script, check=False)
        assert result.returncode and message in result.stderr, (name, result.stderr)
        assert namespace_snapshot(db, "msu_hub_private") == before, name
        assert rows_snapshot(db, ("feature_records", "feature_jobs", "feature_operations")) == feature_rows, name
        rejected_cases.add(name)

    db.run("INSERT INTO msu_hub_private.schema_migrations(version) VALUES(99);")
    reject("backfill_ledger", backfill, "require schema revision 5")
    db.run("DELETE FROM msu_hub_private.schema_migrations WHERE version=99;")
    db.run("""
        INSERT INTO msu_hub_private.feature_records(owner_id,feature,scope_key,collection,key,payload_version,payload)
        VALUES (0,'settings','global','chats','-101',1,'{}');
    """)
    reject("backfill_collision", backfill, "destination is not empty")
    db.run("DELETE FROM msu_hub_private.feature_records WHERE owner_id=0;")
    db.run("UPDATE msu_hub_private.chat_settings SET settings=settings||'{\"with_nsfw\":null}';")
    reject("backfill_invalid_boolean", backfill, "invalid boolean values")
    db.run("UPDATE msu_hub_private.chat_settings SET settings=settings-'with_nsfw';")
    db.run("UPDATE msu_hub_private.chat_settings SET settings=settings||jsonb_build_object('oversized',repeat('x',70000));")
    reject("backfill_oversized_payload", backfill, "check constraint")
    db.run("UPDATE msu_hub_private.chat_settings SET settings=settings-'oversized';")
    reject("backfill_partial_failure", backfill.replace("VALUES(6);", "VALUES(1/0);"), "division by zero")
    assert_locked_writer_rejects_migration(db, backfill)
    rejected_cases.add("backfill_live_writer")
    # Rehearsed invalid source writes themselves journal IDs; the migration must not alter them.
    unchanged = rows_snapshot(db, durable)
    db.run(backfill)
    assert db.value("SELECT msu_hub_private.verify_application_documents();") == {
        "settings": 1,
        "directory": 2,
        "vk_subscriptions": 2,
    }
    assert rows_snapshot(db, durable) == unchanged
    assert (
        db.run("""
        SELECT (payload->'future'->>'precision')::numeric = 1.00000000000000001
        FROM msu_hub_private.feature_records WHERE feature='settings';
    """).stdout.strip()
        == "t"
    )
    for name in ("application_documents_source", "verify_application_documents"):
        assert db.run(f"SELECT msu_hub_private.{name}();", principal=PRINCIPAL, check=False).returncode

    db.run("INSERT INTO msu_hub_private.schema_migrations(version) VALUES(99);")
    reject("retire_ledger", retire, "requires schema revision 6")
    db.run("DELETE FROM msu_hub_private.schema_migrations WHERE version=99;")
    variations = {
        "retire_payload_drift": ("payload=payload||'{\"unexpected\":true}'", "payload=payload-'unexpected'"),
        "retire_version_drift": ("payload_version=2", "payload_version=1"),
        "retire_retention_drift": ("expires_at=now()+interval '1 day'", "expires_at=NULL"),
        "retire_metadata_drift": ("parent='unexpected'", "parent=NULL"),
        "retire_timestamp_drift": ("updated_at=updated_at+interval '1 second'", "updated_at=updated_at-interval '1 second'"),
    }
    for name, (change, restore) in variations.items():
        db.run(f"UPDATE msu_hub_private.feature_records SET {change} WHERE feature='settings';")
        reject(name, retire, "parity check failed")
        db.run(f"UPDATE msu_hub_private.feature_records SET {restore} WHERE feature='settings';")
    db.run("UPDATE msu_hub_private.chat_settings SET settings=settings||'{\"old_writer\":true}';")
    reject("retire_old_writer_drift", retire, "parity check failed")
    db.run("UPDATE msu_hub_private.chat_settings SET settings=settings-'old_writer';")
    db.run("""
        INSERT INTO msu_hub_private.feature_records(owner_id,feature,scope_key,collection,key,payload_version,payload)
        VALUES (0,'settings','global','chats','extra',1,'{}');
    """)
    reject("retire_extra_record", retire, "parity check failed")
    db.run("DELETE FROM msu_hub_private.feature_records WHERE owner_id=0 AND key='extra';")
    db.run("CREATE VIEW msu_hub_private.unreviewed_consumer AS SELECT * FROM msu_hub_private.directory;")
    reject("retire_external_dependency", retire, "other objects depend")
    db.run("DROP VIEW msu_hub_private.unreviewed_consumer;")
    reject("retire_partial_failure", retire.replace("VALUES(7);", "VALUES(1/0);"), "division by zero")
    assert_locked_writer_rejects_migration(db, retire)
    rejected_cases.add("retire_live_writer")
    unchanged = rows_snapshot(db, durable)
    feature_rows = rows_snapshot(db, ("feature_records",))
    db.run(retire)
    assert rows_snapshot(db, durable) == unchanged
    assert rows_snapshot(db, ("feature_records",)) == feature_rows
    assert db.value("SELECT jsonb_agg(version ORDER BY version) FROM msu_hub_private.schema_migrations;") == list(range(1, 8))
    assert (
        db.value("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='msu_hub_private' AND c.relname IN ('chat_settings','directory','vk_subscriptions');
    """)
        == 0
    )
    assert (
        db.value("""
        SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname IN ('msu_hub_private','msu_hub_api') AND
            (p.proname LIKE '%directory%' OR p.proname LIKE '%settings%' OR p.proname LIKE '%vk_%'
                OR p.proname IN ('verify_application_documents','application_documents_source'));
    """)
        == 0
    )
    assert db.value("SELECT count(*) FROM msu_hub_private.mutation_journal WHERE operation='DELETE';") > 0
    assert db.rpc("health") == {"schema_version": 1, "bot_id": 999, "application_documents": 1}
    assert call(db, "health", {}) == {"version": 1}
    asyncio.run(verify_migrated_models(db))
    assert rows_snapshot(db, ("feature_records",)) == feature_rows
    return rejected_cases


@pytest.mark.parametrize(
    "case",
    [
        "backfill_ledger",
        "backfill_collision",
        "backfill_invalid_boolean",
        "backfill_oversized_payload",
        "backfill_partial_failure",
        "backfill_live_writer",
        "retire_ledger",
        "retire_payload_drift",
        "retire_version_drift",
        "retire_retention_drift",
        "retire_metadata_drift",
        "retire_timestamp_drift",
        "retire_old_writer_drift",
        "retire_extra_record",
        "retire_external_dependency",
        "retire_partial_failure",
        "retire_live_writer",
    ],
)
def test_application_migration_rejects_unsafe_changes_atomically(application_postgres, case):
    assert case in application_postgres.application_upgrade


@pytest.mark.parametrize(
    "feature,collection,key,payload",
    [
        ("settings", "chats", "-101", {"auto_speech_recognition": True, "auto_video_links": False, "with_nsfw": False, "future": None}),
        ("ecosystem", "chats", "-101", {"chat_id": -101, "name": "Друзья"}),
        ("vk", "subscriptions", "-5:-101", {"owner_id": -5, "chat_id": -101, "last_post_id": 9}),
    ],
)
def test_application_documents_use_authenticated_shared_generic_api(application_db, feature, collection, key, payload):
    db = application_db
    db.run(f"INSERT INTO msu_hub_private.principals(auth_user_id,bot_id) VALUES('{STRANGER}',888);")
    tx = transaction()
    tx.update(
        feature=feature,
        scope=SCOPE,
        guards=[guard(key, collection=collection)],
        puts=[
            {
                "collection": collection,
                "key": key,
                "payload": payload,
                "payload_version": 1,
                "parent": None,
                "status": None,
                "expires_at": None,
            }
        ],
    )
    created = call(db, "commit", tx)["records"][0]
    request = {"feature": feature, "scope": SCOPE, "collection": collection, "key": key}
    assert call(db, "get", request, STRANGER) == created
    assert created["expires_at"] is None and created["payload"] == payload
    assert call(db, "get", {**request, "scope": {**SCOPE, "owner": "bot"}}) is None
    before = rows_snapshot(db, ("feature_records",))
    db.run("SELECT msu_hub_private.retain_features(100,now()+interval '100 years');")
    assert rows_snapshot(db, ("feature_records",)) == before
    assert db.run("SELECT * FROM msu_hub_private.feature_records;", principal=PRINCIPAL, check=False).returncode
    db.run(f"UPDATE msu_hub_private.principals SET enabled=false WHERE auth_user_id='{STRANGER}';")
    assert db.run(f"SELECT msu_hub_api.feature_get_v1({literal(request)});", principal=STRANGER, check=False).returncode


def test_retirement_preserves_user_chat_journaling(application_db):
    db = application_db
    db.rpc("ensure_chat", literal({"chat_id": -101, "type": "supergroup", "title": "Still observed"}))
    assert db.value("SELECT jsonb_agg(DISTINCT relation_name) FROM msu_hub_private.mutation_journal;") == ["chats"]
    assert db.rpc("get_chat", "-101")["title"] == "Still observed"


def test_chat_snapshot_refresh_policy_is_atomic_and_defaults_to_refresh(application_db):
    db = application_db
    new = {"chat_id": -101, "type": "supergroup", "title": "Fresh", "username": "new_name"}
    old = {"chat_id": -101, "type": "group", "title": "Old snapshot", "username": "old_name"}
    created = db.rpc("ensure_chat", literal(new) + ",false")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: db.rpc("ensure_chat", literal(old) + ",false"), range(8)))
    assert all(result == created for result in results)
    assert db.rpc("get_chat", "-101") == created
    assert db.rpc("ensure_chat", literal({**new, "title": "Newer"}))["title"] == "Newer"
    assert db.run(f"SELECT msu_hub_api.ensure_chat_v1({literal(old)},NULL);", principal=PRINCIPAL, check=False).returncode
    assert db.value("""
        SELECT jsonb_build_array(p.prosecdef,pg_get_userbyid(p.proowner),p.proconfig)
        FROM pg_proc p WHERE p.oid='msu_hub_api.ensure_chat_v1(jsonb,boolean)'::regprocedure;
    """) == [True, "msu_hub_owner", ['search_path=""']]
    assert db.value("SELECT to_jsonb(to_regprocedure('msu_hub_api.ensure_chat_v1(jsonb)') IS NULL);") is True
    assert db.run(f"SELECT msu_hub_api.ensure_chat_v1({literal(old)},false);", principal=STRANGER, check=False).returncode


async def test_application_facade_preserves_identity_and_fields_through_conflicts_and_restart(application_db):
    from msu_hub_bot.storage.models import DirectoryCreate, DirectoryPatch, VkPatch

    db = application_db
    db.rpc("ensure_chat", literal({"chat_id": -101, "type": "supergroup", "title": "Synthetic"}))
    db.run('UPDATE msu_hub_private.chats SET metadata=\'{"settings":{"future":{"keep":[1,null]}}}\';')
    documents, backend = document_repository(db, lose_response=True)
    observed = await documents.chat_lookup(-101)
    await documents.load_settings(observed)
    assert backend.requests[0] == backend.requests[1]
    await asyncio.gather(
        documents.patch_settings(-101, {"with_nsfw": True}),
        documents.patch_settings(-101, {"auto_video_links": False}),
        documents.patch_settings(-101, {"nullable_extra": None}),
    )
    original = await documents.create_directory(DirectoryCreate(chat_id=-999, name="Independent"))
    assert await documents.create_directory(DirectoryCreate(chat_id=-999, name="Duplicate")) == original
    edited = await documents.patch_directory(-999, DirectoryPatch(members=17, username_alias=None))
    assert edited.id == original.id and edited.created == original.created
    subscription = await documents.upsert_vk_subscription(-5, -999, VkPatch(description="Saved"))
    await asyncio.gather(*(documents.advance_vk_cursor(-5, -999, value) for value in [9, 123, 17, 101]))
    documents, _ = document_repository(db)
    assert await documents.load_settings(observed) == {
        "auto_speech_recognition": True,
        "auto_video_links": False,
        "with_nsfw": True,
        "future": {"keep": [1, None]},
        "nullable_extra": None,
    }
    assert await documents.get_directory(-999) == edited
    [saved] = await documents.list_vk_subscriptions()
    assert saved.id == subscription.id and saved.created == subscription.created and saved.last_post_id == 123
    reset = await documents.upsert_vk_subscription(-5, -999, VkPatch(last_post_id=0, description=None))
    assert reset.last_post_id == 0 and reset.description is None and reset.id == subscription.id
    assert await documents.delete_directory(-999)
    assert not await documents.delete_directory(-999)
    assert await documents.list_directory() == []
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_records WHERE expires_at IS NOT NULL;") == 0
    assert db.value("SELECT count(*) FROM msu_hub_private.mutation_journal WHERE relation_name NOT IN ('users','chats');") == 0


def test_historical_import_and_normalization_refuse_current_schema_before_any_writes(application_db, tmp_path):
    from test_migrate_storage import AS_OF, make_export, source_records
    from test_postgres_storage import migration_target

    from tools.migrate_storage import MigrationError, batch_script, normalization_script

    db = application_db
    directory = tmp_path / "historical-restore"
    manifest = make_export(directory)
    target = migration_target(db, directory)
    with pytest.raises(MigrationError, match="historical_restore_requires_schema_5"):
        target.guard()
    before = rows_snapshot(db, ("users", "chats", "updates", "messages", "feature_records", "mutation_journal"))
    scripts = [batch_script("users", source_records()["users"], manifest), normalization_script([], 999, AS_OF)]
    for script in scripts:
        result = db.run(script, check=False)
        assert result.returncode and "Historical restore requires schema revision 5" in result.stderr
        assert rows_snapshot(db, ("users", "chats", "updates", "messages", "feature_records", "mutation_journal")) == before

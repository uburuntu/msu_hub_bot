"""Feedback excerpts obey the actual authenticated PostgreSQL scope and retention."""

import json

import pytest

from msu_hub_bot.storage.models import FeedbackMessageRecord
from test_postgres_storage import PRINCIPAL, STRANGER, literal

CANARY = "PRIVATE_UNSELECTED_RAW_FIELD"


def exercise_feedback_context_migration(db, migration):
    def snapshot():
        return db.value("""SELECT jsonb_build_object(
            'functions',(SELECT jsonb_agg(jsonb_build_array(oid,prosrc,proowner,proacl,proconfig) ORDER BY oid)
                FROM pg_proc WHERE pronamespace IN ('msu_hub_private'::regnamespace,'msu_hub_api'::regnamespace)),
            'ledger',(SELECT jsonb_agg(version ORDER BY version) FROM msu_hub_private.schema_migrations),
            'messages',(SELECT jsonb_agg(to_jsonb(m)) FROM msu_hub_private.messages m));""")

    before = snapshot()
    db.run("INSERT INTO msu_hub_private.schema_migrations(version) VALUES(99);")
    rejected = db.run(migration, check=False)
    assert rejected.returncode and "requires schema revision 9" in rejected.stderr
    db.run("DELETE FROM msu_hub_private.schema_migrations WHERE version=99;")
    assert db.run(migration.replace("VALUES(10);", "VALUES(1/0);"), check=False).returncode
    assert snapshot() == before
    db.run(migration)
    after = snapshot()
    assert after["ledger"] == list(range(1, 11))
    assert after["messages"] == before["messages"]
    old_functions = {item[0]: item for item in before["functions"]}
    assert {item[0]: item for item in after["functions"] if item[0] in old_functions} == old_functions
    return True


def seed(
    db,
    message_id,
    *,
    bot=999,
    chat=-101,
    thread=None,
    business="",
    age="2 hours",
    observed_age=None,
    data=None,
    sender_user=42,
    sender_chat=None,
):
    payload = {
        "text": f"Message {message_id}",
        "from": {"first_name": "Synthetic", "last_name": "Author", "username": CANARY},
        "unknown": {"token": CANARY},
        **(data or {}),
    }

    def null(value):
        return "NULL" if value is None else str(value)

    db.run(f"""INSERT INTO msu_hub_private.messages(bot_id,chat_id,message_id,business_connection_id,
        sent_at,observed_at,sender_user_id,sender_chat_id,thread_id,data)
        VALUES({bot},{chat},{message_id},'{business}',now()-interval '{age}',now()-interval '{observed_age or age}',
            {null(sender_user)},{null(sender_chat)},{null(thread)},{literal(payload)});""")


def read(db, *, chat=-101, thread=None, principal=PRINCIPAL):
    result = db.rpc(
        "recent_feedback_messages", f"{chat},{'NULL' if thread is None else thread},now()-interval '1 hour',100", principal=principal
    )
    return [FeedbackMessageRecord.model_validate(item) for item in result]


def test_migration_preserves_archive_and_existing_contracts(application_postgres):
    assert application_postgres.feedback_context_upgrade


def test_scope_ignores_raw_threads_for_ordinary_replies_and_excludes_other_bot_chat_business(application_db):
    db = application_db
    seed(db, 1, thread=777)
    seed(db, 2, thread=7, data={"is_topic_message": True})
    seed(db, 3, thread=8, data={"is_topic_message": True})
    seed(db, 4, chat=-102)
    seed(db, 5, bot=1000)
    seed(db, 6, business="business")
    seed(db, 7, data={"direct_messages_topic": {"topic_id": 4}})
    ordinary = read(db)
    assert [item.message_id for item in ordinary] == [1] and ordinary[0].thread_id is None
    assert [item.message_id for item in read(db, thread=7)] == [2]
    assert [item.message_id for item in read(db, thread=8)] == [3]
    assert CANARY not in json.dumps([item.model_dump(mode="json") for item in ordinary])


def test_five_newest_before_invocation_are_chronological_and_retention_applies_before_cleanup(application_db):
    db = application_db
    for index in range(1, 9):
        seed(db, index)
    seed(db, 20, age="30 days")
    seed(db, 21, age="31 days")
    seed(db, 22, age="30 minutes")
    seed(db, 23, observed_age="30 minutes")
    seed(db, 100)
    seed(db, 101)
    assert [item.message_id for item in read(db)] == [4, 5, 6, 7, 8]
    assert db.value("SELECT count(*) FROM msu_hub_private.messages WHERE message_id=21;") == 1


def test_only_caption_attribution_media_kind_and_bounded_text_escape_archive(application_db):
    db = application_db
    seed(
        db,
        1,
        sender_chat=-999,
        data={
            "text": None,
            "caption": "😀" * 900,
            "sender_chat": {"title": "Channel " * 30},
            "photo": [{"file_id": CANARY}],
            "document": {"file_name": CANARY},
        },
    )
    result = read(db)[0]
    assert result.text == "😀" * 800 and result.truncated
    assert result.media_kind == "photo" and result.author_kind == "chat" and result.author_id == -999
    assert len(result.author_name) == 128 and CANARY not in result.model_dump_json()


def test_prior_same_second_message_is_available_at_frozen_capture_time(application_db):
    db = application_db
    second = db.value("SELECT to_jsonb(date_trunc('second',now())-interval '1 minute');")
    seed(db, 99)
    db.run(f"""UPDATE msu_hub_private.messages SET sent_at='{second}',
        observed_at='{second}'::timestamptz+interval '0.2 seconds' WHERE message_id=99;""")
    result = db.rpc("recent_feedback_messages", f"-101,NULL,'{second}'::timestamptz+interval '0.8 seconds',100")
    assert [message["message_id"] for message in result] == [99]


def test_archived_rich_message_returns_kind_without_exposing_its_tree(application_db):
    seed(application_db, 1, data={"text": None, "rich_message": {"blocks": [{"type": "paragraph", "text": CANARY}]}})
    result = read(application_db)[0]
    assert result.media_kind == "rich_message" and result.text == ""
    assert CANARY not in result.model_dump_json()


def test_principal_gate_and_grants_keep_raw_tables_private(application_db):
    db = application_db
    sql = "SELECT msu_hub_api.recent_feedback_messages_v1(-101,NULL,now(),100);"
    assert db.run(sql, principal=STRANGER, check=False).returncode
    assert db.run("SET ROLE anon; " + sql, check=False).returncode
    assert db.run("SELECT * FROM msu_hub_private.messages;", principal=PRINCIPAL, check=False).returncode
    seed(db, 1)
    db.run(f"INSERT INTO msu_hub_private.principals(auth_user_id,bot_id) VALUES('{STRANGER}',1000);")
    assert read(db, principal=STRANGER) == []
    db.run(f"UPDATE msu_hub_private.principals SET enabled=false WHERE auth_user_id='{PRINCIPAL}';")
    assert db.run(sql, principal=PRINCIPAL, check=False).returncode


@pytest.mark.parametrize(
    "arguments",
    [
        "NULL,NULL,now(),1",
        "0,NULL,now(),1",
        "-101,0,now(),1",
        "-101,-1,now(),1",
        "-101,NULL,NULL,1",
        "-101,NULL,'infinity',1",
        "-101,NULL,now(),NULL",
        "-101,NULL,now(),0",
    ],
)
def test_invalid_scope_is_rejected(application_db, arguments):
    result = application_db.run(f"SELECT msu_hub_api.recent_feedback_messages_v1({arguments});", principal=PRINCIPAL, check=False)
    assert result.returncode and "Invalid feedback context scope" in result.stderr

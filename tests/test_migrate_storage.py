"""Synthetic administrative transfer contracts; no live configuration is read."""

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import edgedb
import pytest

from tools import migrate_storage as migration


def source_schema(table):
    names = migration.COLUMNS[table].split()
    if table in {"users", "chats"}:
        names.append("full_name")
    return {
        "name": migration.TABLES[table],
        "links": [{"name": "__type__"}],
        "properties": [
            {
                "name": name,
                "cardinality": "One",
                "required": name in {"id", "created"},
                "target": {"name": migration.SOURCE_TYPES.get(name, "std::str")},
            }
            for name in names
        ],
    }


def source_records():
    created = "2026-01-02T03:04:05.123456Z"
    common = {"created": created}
    return {
        "users": [
            {
                **common,
                "id": str(UUID(int=1)),
                "user_id": 2**62,
                "is_bot": False,
                "first_name": "Друг",
                "last_name": None,
                "username": None,
                "language_code": "ru",
                "full_name": "Друг",
                "metadata": {"precise": Decimal("0.1234567890123456789012345678901234"), "settings": [None, "{}"]},
            }
        ],
        "chats": [
            {
                **common,
                "id": str(UUID(int=2)),
                "chat_id": -(2**62),
                "type": "supergroup",
                "title": "Чат",
                "username": None,
                "first_name": None,
                "last_name": None,
                "full_name": "Чат",
                "metadata": {"settings": {"future": {"unknown": [Decimal("1.00000000000000001"), None]}}},
            }
        ],
        "directory": [
            {
                **common,
                "id": str(UUID(int=3)),
                "chat_id": -42,
                "name": "Friends",
                "section": "other",
                "is_hidden": False,
                "username_alias": None,
                "members": None,
                "pinned_message_id": 2**30,
            }
        ],
        "vk_subscriptions": [
            {
                **common,
                "id": str(UUID(int=4)),
                "chat_id": -42,
                "owner_id": -15,
                "last_post_id": 2147483647,
                "with_reposts": True,
                "with_header": False,
                "is_suspended": False,
                "description": None,
            }
        ],
        "updates": [
            {**common, "id": str(UUID(int=5)), "handled": True, "data": None},
            {
                **common,
                "id": str(UUID(int=6)),
                "handled": False,
                "data": {"precision": Decimal("12345678901234567890.123456789"), "text": "\\.\n\\t\t'private-marker'"},
            },
        ],
    }


class Source:
    def __init__(self, records=None):
        self.records = records or source_records()
        self.queries = []

    def query_single(self, query):
        self.queries.append(query)
        if "get_current_database" in query:
            return "source_test"
        if "datetime_current" in query:
            return datetime(2026, 9, 17, tzinfo=UTC)
        for table, type_name in migration.TABLES.items():
            if f"count({type_name})" in query:
                return len(self.records[table])
        raise AssertionError(query)

    def query_json(self, query, **parameters):
        self.queries.append(query)
        if query == migration.SCHEMA_QUERY:
            return migration.canonical([source_schema(table) for table in migration.TABLES])
        for table, type_name in migration.TABLES.items():
            if f"SELECT {type_name} " in query:
                after = str(parameters.get("after", ""))
                return migration.canonical([row for row in self.records[table] if row["id"] > after][: parameters["limit"]])
        raise AssertionError(query)


def make_export(directory, records=None):
    directory.mkdir(mode=0o700)
    source = Source(records)
    manifest = migration.export_snapshot(source, directory, 999, 1)
    migration.save_json(directory / "manifest.json", manifest)
    return manifest


@pytest.mark.parametrize("text", ["0.0000000000000000001234567890123456789", "1000000000000000000.000000000000000001", "1e30", "-0.00"])
def test_arbitrary_json_precision_is_exact(text):
    decoded = migration.decode('{"value":' + text + "}")
    assert isinstance(decoded["value"], Decimal)
    assert migration.decode(migration.canonical(decoded))["value"] == Decimal(text)


@pytest.mark.parametrize("text", ['{"key":1,"key":2}', '{"v":NaN}', '{"v":Infinity}'])
def test_ambiguous_or_nonfinite_json_is_not_silently_changed(text):
    with pytest.raises(migration.MigrationError):
        migration.decode(text)


def test_snapshot_uses_all_introspected_fields_and_uuid_keyset(tmp_path, capsys):
    directory = tmp_path / "export"
    manifest = make_export(directory)
    assert migration.verify_export(directory) == manifest
    assert manifest["tables"]["updates"]["count"] == 2
    assert "metadata" in manifest["tables"]["users"]["fields"]
    assert "full_name" in manifest["tables"]["chats"]["fields"]
    assert (
        Decimal("0.1234567890123456789012345678901234")
        == next(migration.rows(directory, "users", source_schema("users")))["metadata"]["precise"]
    )
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())
    assert "private-marker" not in capsys.readouterr().out


def test_export_wraps_every_read_in_one_nonretrying_readonly_transaction(monkeypatch, tmp_path):
    source = Source()
    transaction_entries = []
    options = {}

    class Transaction:
        def __enter__(self):
            transaction_entries.append("begin")
            return self

        def __exit__(self, *args):
            transaction_entries.append("commit")

        def query_single(self, query):
            assert transaction_entries == ["begin"]
            return source.query_single(query)

        def query_json(self, query, **values):
            assert transaction_entries == ["begin"]
            return source.query_json(query, **values)

    class Client:
        def with_transaction_options(self, value):
            options["transaction"] = value.start_transaction_query()
            return self

        def with_retry_options(self, value):
            options["attempts"] = value.get_rule_for_exception(Exception()).attempts
            return self

        def transaction(self):
            yield Transaction()

        def close(self):
            transaction_entries.append("close")

    monkeypatch.setattr(edgedb, "create_client", lambda **kwargs: Client())
    migration.export({"connection": {"host": "source.invalid"}, "expected_database": "source_test"}, tmp_path / "snapshot", 999, 1)
    assert transaction_entries == ["begin", "commit", "close"]
    assert options == {"transaction": "START TRANSACTION ISOLATION SERIALIZABLE, READ ONLY, DEFERRABLE;", "attempts": 1}


def test_corrupt_or_incomplete_export_is_rejected_before_import(tmp_path):
    directory = tmp_path / "export"
    make_export(directory)
    path = directory / "updates.jsonl"
    path.write_text(path.read_text().replace("private-marker", "tampered-marker"))
    with pytest.raises(migration.MigrationError, match="checksum"):
        migration.verify_export(directory)


def test_unknown_actual_scalar_is_exportable_but_import_requires_explicit_mapping():
    schema = source_schema("users")
    schema["properties"].append({"name": "future_property", "cardinality": "One", "target": {"name": "std::str"}})
    assert "future_property" in migration.fields(schema)
    with pytest.raises(migration.MigrationError, match="unmapped_source_fields"):
        migration.import_fields("users", schema)


def test_changed_scalar_type_requires_explicit_import_mapping():
    schema = source_schema("users")
    next(item for item in schema["properties"] if item["name"] == "first_name")["target"]["name"] = "std::json"
    with pytest.raises(migration.MigrationError, match="unmapped_source_scalar_type"):
        migration.import_fields("users", schema)


def test_copy_escape_preserves_literal_backslashes_newlines_and_end_marker():
    line = migration.canonical({"text": "\\.\n\t\\t\r\x01", "number": Decimal("1.23456789123456789123456789")})
    assert migration.copy_unescape(migration.copy_escape(line)) == line
    assert "\n" not in migration.copy_escape(line)


def test_resume_replays_only_unacknowledged_transaction(tmp_path):
    directory = tmp_path / "export"
    manifest = make_export(directory)
    scripts = []

    def run(script):
        scripts.append(script)
        if len(scripts) == 2:
            raise migration.MigrationError("commit_acknowledgement_lost")

    target = SimpleNamespace(fingerprint="test", guard=lambda: None, run=run)
    with pytest.raises(migration.MigrationError, match="acknowledgement"):
        migration.import_data(target, directory, manifest, 1)
    target.run = scripts.append
    migration.import_data(target, directory, manifest, 1)
    assert scripts[1] == scripts[2]
    assert sum("hub_private.users AS existing" in script for script in scripts) == 1
    assert not any("TRUNCATE" in script or "DELETE FROM" in script for script in scripts)


def test_per_field_reconciliation_reports_values_without_exposing_them():
    source = source_records()["users"]
    target = copy.deepcopy(source)
    target[0]["metadata"]["precise"] = Decimal("0.1234567890123456789012345678901235")
    result = migration.compare_rows(source, target, source_schema("users"))
    assert result["field_mismatches"] == {"metadata": 1}
    assert result["expected"]["fields"]["metadata"] != result["actual"]["fields"]["metadata"]
    assert "Друг" not in json.dumps(result, ensure_ascii=False)
    assert "0.123456" not in json.dumps(result)


def test_target_credentials_only_enter_environment(monkeypatch, tmp_path):
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0, stdout="{}")

    monkeypatch.setattr(migration.subprocess, "run", run)
    target = migration.Postgres(
        {
            "connection": {"PGDATABASE": "postgres", "PGPASSWORD": "private-canary"},
            "expected_database": "postgres",
            "expected_system_identifier": "123",
        },
        tmp_path,
        999,
    )
    target.run("SELECT 1;")
    assert "private-canary" not in repr(captured["command"])
    assert captured["env"]["PGPASSWORD"] == "private-canary"


def test_private_config_required(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"password":"private-canary"}')
    path.chmod(0o644)
    with pytest.raises(migration.MigrationError, match="private_configuration_required"):
        migration.private_json(path)


AS_OF = "2026-09-17T00:00:00Z"


def message_records():
    records = source_records()
    user = {"id": records["users"][0]["user_id"], "is_bot": False, "first_name": "Друг"}
    chat = {"id": records["chats"][0]["chat_id"], "type": "supergroup", "title": "Чат"}

    def message(identifier, date, text, **extra):
        return {
            "message_id": identifier,
            "date": int(datetime.fromisoformat(date).timestamp()),
            "chat": chat,
            "from": user,
            "text": text,
            **extra,
        }

    child = message(41, "2026-08-19T12:00:00Z", "child-body")
    parent = message(42, "2026-09-16T12:00:00Z", "parent-body", reply_to_message=child, opaque=Decimal("0.12345678901234567890123456789"))
    records["updates"] = [
        {"id": str(UUID(int=5)), "created": "2026-09-16T12:01:00Z", "handled": True, "data": {"update_id": 10, "message": parent}},
        {
            "id": str(UUID(int=6)),
            "created": "2026-09-16T14:00:00Z",
            "handled": True,
            "data": {
                "update_id": 11,
                "edited_message": {
                    **parent,
                    "text": "edited-parent-body",
                    "edit_date": int(datetime(2026, 9, 16, 13, tzinfo=UTC).timestamp()),
                },
            },
        },
        {
            "id": str(UUID(int=7)),
            "created": "2020-01-01T12:01:00Z",
            "handled": False,
            "data": {"update_id": 12, "message": message(40, "2020-01-01T12:00:00Z", "expired-body")},
        },
    ]
    return records


def test_normalization_preserves_precise_bodies_and_separates_nested_message_lifetimes():
    row = message_records()["updates"][0]
    result = migration.transform_update(row, AS_OF)
    assert result["id"] == row["id"]
    assert "parent-body" not in migration.canonical(result["data"])
    parent, child = result["messages"]
    assert parent["data"]["opaque"] == Decimal("0.12345678901234567890123456789")
    assert parent["data"]["text"] == "parent-body"
    assert "text" not in parent["data"]["reply_to_message"]
    assert child["data"]["text"] == "child-body"
    assert migration.transform_update(message_records()["updates"][2], AS_OF)["messages"] == []


def test_eligible_unknown_nested_message_is_reported_not_silently_lost():
    row = message_records()["updates"][0]
    row["data"]["future_message"] = {**row["data"]["message"], "message_id": 88}
    with pytest.raises(migration.MigrationError, match="eligible_legacy_message_not_extracted"):
        migration.transform_update(row, AS_OF)


def test_nullable_business_identity_and_iso_dates_keep_original_wire_values():
    row = message_records()["updates"][0]
    row["data"]["message"]["business_connection_id"] = None
    row["data"]["message"]["date"] = "2026-09-16T12:00:00Z"
    result = migration.transform_update(row, AS_OF)
    assert result["messages"][0]["business_connection_id"] == ""
    assert result["messages"][0]["data"]["business_connection_id"] is None
    assert result["messages"][0]["data"]["date"] == "2026-09-16T12:00:00Z"


def test_malformed_receipts_have_private_reject_artifacts_and_block_application(tmp_path):
    directory = tmp_path / "export"
    manifest = make_export(directory)
    result = migration.prepare_normalization(directory, manifest, AS_OF)
    assert sum(result["rejections"].values()) == 2
    rejects = migration.normalization_path(directory, AS_OF) / "rejects.jsonl"
    assert rejects.stat().st_mode & 0o077 == 0
    assert len(list(migration.json_lines(rejects))) == 2
    with pytest.raises(migration.MigrationError, match="rejection_review"):
        migration.verify_normalization(directory, manifest, AS_OF)


@pytest.mark.parametrize(
    "command", [["psql", "--password=private-canary"], ["docker", "exec", "-i", "-e", "PGPASSWORD=private-canary", "db", "psql"]]
)
def test_target_command_never_embeds_credentials(tmp_path, command):
    with pytest.raises(migration.MigrationError, match="not_allowlisted"):
        migration.Postgres(
            {
                "connection": {"PGDATABASE": "postgres"},
                "expected_database": "postgres",
                "expected_system_identifier": "123",
                "psql_command": command,
            },
            tmp_path,
            999,
        )

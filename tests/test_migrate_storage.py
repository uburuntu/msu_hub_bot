"""Synthetic administrative transfer contracts; no live configuration is read."""

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4
from pathlib import Path
from typing import Any

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


def make_export(
    directory: Path, records: dict[str, list[dict[str, Any]]] | None = None, *, updates_since: str | None = None
) -> dict[str, Any]:
    """Build the archived file format directly, without a live source database."""
    directory.mkdir(mode=0o700)
    records = records if records is not None else source_records()
    cutoff = migration.utc(updates_since) if updates_since is not None else None
    manifest: dict[str, Any] = {
        "format": migration.FORMAT,
        "export_id": str(uuid4()),
        "bot_id": 999,
        "snapshot_at": "2026-09-17T00:00:00.000000Z",
        "consistency": "single_readonly_transaction",
        "selection": {"updates": {"field": "created", "operator": ">", "value": cutoff} if cutoff is not None else None},
        "tables": {},
    }
    for table in migration.TABLES:
        schema = source_schema(table)
        digest = migration.Digests(migration.fields(schema))
        selected = [
            migration.normalized_row(copy.deepcopy(row), schema)
            for row in records[table]
            if table != "updates" or cutoff is None or migration.utc(row["created"]) > cutoff
        ]
        path = directory / (table + ".jsonl")
        with path.open("x", encoding="utf-8") as output:
            path.chmod(0o600)
            output.writelines(digest.add(row) for row in selected)
        manifest["tables"][table] = {
            "schema": schema,
            "source_count": len(records[table]),
            "excluded_count": len(records[table]) - len(selected),
            **digest.result(),
        }
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


def test_archive_verification_preserves_fields_precision_and_private_permissions(tmp_path, capsys):
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


def test_corrupt_or_incomplete_export_is_rejected_before_import(tmp_path):
    directory = tmp_path / "export"
    make_export(directory)
    path = directory / "updates.jsonl"
    path.write_text(path.read_text().replace("private-marker", "tampered-marker"))
    with pytest.raises(migration.MigrationError, match="checksum"):
        migration.verify_export(directory)


def test_filtered_export_preserves_all_durable_rows_and_strict_timestamp_boundary(tmp_path):
    records = source_records()
    base = records["updates"][1]
    records["updates"] = [
        {**base, "id": str(UUID(int=5 + index)), "created": stamp}
        for index, stamp in enumerate(
            [
                "2026-08-17T23:59:59.999999Z",
                "2026-08-18T03:00:00+03:00",
                "2026-08-18T00:00:00.000001Z",
                "2026-09-16T00:00:00Z",
            ]
        )
    ]
    full_path, filtered_path = tmp_path / "full", tmp_path / "filtered"
    full = make_export(full_path, records)
    filtered = make_export(filtered_path, records, updates_since="2026-08-18T03:00:00+03:00")
    assert migration.verify_export(filtered_path) == filtered
    assert filtered["selection"] == {"updates": {"field": "created", "operator": ">", "value": "2026-08-18T00:00:00.000000Z"}}
    assert migration.selection_summary(filtered) == {
        "updates_since": "2026-08-18T00:00:00.000000Z",
        "source_total": 4,
        "selected": 2,
        "excluded": 2,
    }
    for table in migration.TABLES.keys() - {"updates"}:
        assert filtered["tables"][table] == full["tables"][table]
        assert (filtered_path / (table + ".jsonl")).read_bytes() == (full_path / (table + ".jsonl")).read_bytes()
    selected = list(migration.rows(filtered_path, "updates", source_schema("updates")))
    assert [row["id"] for row in selected] == [str(UUID(int=7)), str(UUID(int=8))]
    assert all(row["data"] == base["data"] for row in selected)


def test_filtered_export_can_select_zero_receipts_without_losing_durable_rows(tmp_path):
    directory = tmp_path / "empty-window"
    manifest = make_export(directory, updates_since="2026-08-18T00:00:00Z")
    assert migration.verify_export(directory) == manifest
    assert manifest["tables"]["updates"]["count"] == 0
    assert manifest["tables"]["updates"]["source_count"] == manifest["tables"]["updates"]["excluded_count"] == 2
    assert all(manifest["tables"][table]["count"] for table in migration.TABLES.keys() - {"updates"})


@pytest.mark.parametrize("change", ["operator", "future", "missing", "count", "durable", "downgrade"])
def test_malformed_selection_manifests_are_rejected(tmp_path, change):
    directory = tmp_path / "export"
    manifest = make_export(directory, updates_since="2026-01-01T00:00:00Z")
    if change == "operator":
        manifest["selection"]["updates"]["operator"] = ">="
    elif change == "future":
        manifest["selection"]["updates"]["value"] = "2027-01-01T00:00:00.000000Z"
    elif change == "missing":
        del manifest["selection"]
    elif change == "count":
        manifest["tables"]["updates"]["excluded_count"] = 1
    elif change == "durable":
        manifest["tables"]["users"]["source_count"] += 1
        manifest["tables"]["users"]["excluded_count"] = 1
    else:
        manifest["format"] = 1
    migration.save_json(directory / "manifest.json", manifest)
    with pytest.raises(migration.MigrationError, match="manifest_selection"):
        migration.verify_export(directory)


def test_verifier_checks_selected_rows_even_when_their_hashes_match(tmp_path):
    directory = tmp_path / "export"
    manifest = make_export(directory, updates_since="2026-01-01T00:00:00Z")
    schema = source_schema("updates")
    rows = list(migration.rows(directory, "updates", schema))
    rows[0]["created"] = "2025-12-31T00:00:00.000000Z"
    digests = migration.Digests(migration.fields(schema))
    (directory / "updates.jsonl").write_text("".join(digests.add(row) for row in rows))
    manifest["tables"]["updates"].update(digests.result())
    migration.save_json(directory / "manifest.json", manifest)
    with pytest.raises(migration.MigrationError, match="export_selection_mismatch"):
        migration.verify_export(directory)


def test_original_full_export_format_remains_readable_and_importable(tmp_path):
    directory = tmp_path / "legacy"
    manifest = make_export(directory)
    manifest["format"] = 1
    del manifest["selection"]
    for entry in manifest["tables"].values():
        del entry["source_count"], entry["excluded_count"]
    migration.save_json(directory / "manifest.json", manifest)
    assert migration.verify_export(directory) == manifest
    assert migration.selection_summary(manifest)["excluded"] == 0
    scripts = []
    target = SimpleNamespace(fingerprint="test", guard=lambda: None, run=scripts.append)
    migration.import_data(target, directory, manifest, 1)
    first_count = len(scripts)
    migration.import_data(target, directory, manifest, 1)
    assert len(scripts) == first_count == 6


def test_import_resume_rejects_changed_selection_even_with_same_rows_and_export_id(tmp_path):
    directory = tmp_path / "export"
    manifest = make_export(directory, updates_since="2026-01-01T00:00:00Z")
    scripts = []
    target = SimpleNamespace(fingerprint="test", guard=lambda: None, run=scripts.append)
    migration.import_data(target, directory, manifest, 1)
    manifest["selection"]["updates"]["value"] = "2025-12-31T00:00:00.000000Z"
    with pytest.raises(migration.MigrationError, match="resume_manifest_mismatch"):
        migration.import_data(target, directory, manifest, 1)
    assert len(scripts) == 6


def test_unknown_archived_scalar_requires_explicit_import_mapping():
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
    assert sum("msu_hub_private.users AS existing" in script for script in scripts) == 1
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


@pytest.mark.parametrize("revision", [4, 6, 7, 99, True, "5", None])
def test_historical_restore_cannot_be_retargeted_to_another_schema(tmp_path, monkeypatch, revision):
    monkeypatch.setattr(migration.subprocess, "run", lambda *args, **kwargs: pytest.fail("Must reject before connecting"))
    with pytest.raises(migration.MigrationError, match="historical_restore_requires_schema_5"):
        migration.Postgres({"expected_schema_version": revision}, tmp_path, 999)


@pytest.mark.parametrize("revision", [5, 6, 7])
def test_historical_restore_checks_actual_revision_even_with_valid_configuration(tmp_path, monkeypatch, revision):
    target = migration.Postgres(
        {
            "connection": {"PGDATABASE": "hub_test_restore"},
            "expected_database": "hub_test_restore",
            "expected_system_identifier": "123",
            "expected_schema_version": 5,
        },
        tmp_path,
        999,
    )
    result = {"database": "hub_test_restore", "cluster": "123", "principal": True, "version": revision}
    monkeypatch.setattr(target, "run", lambda script: migration.canonical(result))
    if revision == 5:
        target.guard()
    else:
        with pytest.raises(migration.MigrationError, match="historical_restore_requires_schema_5"):
            target.guard()


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


def test_normalization_requires_complete_retention_coverage_and_binds_selection(tmp_path):
    directory = tmp_path / "export"
    manifest = make_export(directory, message_records(), updates_since="2026-08-19T00:00:00Z")
    with pytest.raises(migration.MigrationError, match="export_does_not_cover_retention_window"):
        migration.prepare_normalization(directory, manifest, AS_OF)
    assert not list(directory.glob("normalization-*"))
    covered_as_of = "2026-09-18T00:00:00Z"
    report = migration.prepare_normalization(directory, manifest, covered_as_of)
    assert report["counts"]["source_rows"] == report["counts"]["accepted"] == 2
    assert report["selection"]["source_total"] == 3 and report["selection"]["excluded"] == 1
    migration.verify_normalization(directory, manifest, covered_as_of)
    manifest["selection"]["updates"]["value"] = "2026-08-18T00:00:00.000000Z"
    with pytest.raises(migration.MigrationError, match="normalization_manifest_mismatch"):
        migration.verify_normalization(directory, manifest, covered_as_of)


def test_legacy_full_normalization_report_remains_verifiable(tmp_path):
    directory = tmp_path / "legacy"
    manifest = make_export(directory, message_records())
    manifest["format"] = 1
    del manifest["selection"]
    for entry in manifest["tables"].values():
        del entry["source_count"], entry["excluded_count"]
    migration.save_json(directory / "manifest.json", manifest)
    report = migration.prepare_normalization(directory, manifest, AS_OF)
    report["format"] = 1
    del report["export_manifest_sha256"], report["selection"]
    migration.save_json(migration.normalization_path(directory, AS_OF) / "manifest.json", report)
    assert migration.verify_normalization(directory, manifest, AS_OF)[1] == report


@pytest.mark.parametrize("change", ["selection_hash", "retained_only", "normalized"])
def test_normalization_requires_raw_parity_for_the_identical_selection(tmp_path, change):
    directory = tmp_path / "export"
    manifest = make_export(directory, message_records(), updates_since="2026-08-18T00:00:00Z")
    migration.prepare_normalization(directory, manifest, AS_OF)
    parity = {"export_id": manifest["export_id"], "exact": True, "export_manifest_sha256": migration.manifest_digest(manifest)}
    if change == "selection_hash":
        parity["export_manifest_sha256"] = "0" * 64
    else:
        parity[change] = True
    migration.save_json(directory / "reconciliation-test.json", parity)
    with pytest.raises(migration.MigrationError, match="verified_raw_parity_required"):
        migration.normalize(SimpleNamespace(fingerprint="test"), directory, manifest, AS_OF, 1)


def test_filtered_normalization_resume_is_bound_to_the_full_manifest(tmp_path):
    directory = tmp_path / "export"
    manifest = make_export(directory, message_records(), updates_since="2026-08-18T00:00:00Z")
    report = migration.prepare_normalization(directory, manifest, AS_OF)
    migration.save_json(
        directory / "reconciliation-test.json",
        {
            "export_id": manifest["export_id"],
            "exact": True,
            "export_manifest_sha256": migration.manifest_digest(manifest),
        },
    )
    migration.save_json(
        migration.normalization_path(directory, AS_OF) / "applied-test.json",
        {
            "completed": 1,
            "batch_size": 1,
            "sha256": report["sha256"],
            "manifest": "0" * 64,
        },
    )
    with pytest.raises(migration.MigrationError, match="normalization_resume_mismatch"):
        migration.normalize(SimpleNamespace(fingerprint="test", guard=lambda: None), directory, manifest, AS_OF, 1)


@pytest.mark.parametrize(
    "arguments",
    [
        ["export"],
        ["verify", "--source-config", "source.json"],
        ["verify", "--bot-id", "999"],
        ["verify", "--updates-since", "2026-08-18T00:00:00Z"],
    ],
)
def test_source_database_export_options_are_rejected(monkeypatch, tmp_path, capsys, arguments):
    monkeypatch.setattr(
        migration.sys,
        "argv",
        ["migrate_storage", *arguments, "--directory", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as caught:
        migration.main()
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


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


@pytest.mark.parametrize("extra", [{}, {"type": "channel"}, {"old_reaction": [], "new_reaction": []}, {"reactions": []}])
def test_eligible_unknown_nested_message_is_reported_not_silently_lost(extra):
    row = message_records()["updates"][0]
    message = row["data"]["message"]
    row["data"]["future_message"] = {
        "chat": message["chat"],
        "date": message["date"],
        "message_id": 88,
        "text": "Synthetic future message",
        **extra,
    }
    with pytest.raises(migration.MigrationError, match="eligible_legacy_message_not_extracted"):
        migration.transform_update(row, AS_OF)


@pytest.mark.parametrize("nested", [False, True])
def test_forward_origin_is_preserved_as_a_reference_without_inventing_a_message(nested):
    row = message_records()["updates"][0]
    message = row["data"]["message"]
    if nested:
        message = message["reply_to_message"]
    origin = {
        "type": "channel",
        "date": message["date"],
        "chat": {"id": -800, "type": "channel", "title": "Synthetic origin"},
        "message_id": 900,
        "author_signature": "Synthetic author",
    }
    message["forward_origin"] = origin
    result = migration.transform_update(row, AS_OF)
    assert len(result["messages"]) == 2
    body = next(item["data"] for item in result["messages"] if item["message_id"] == message["message_id"])
    assert body["forward_origin"] == origin
    assert all(item["message_id"] != origin["message_id"] for item in result["messages"])


@pytest.mark.parametrize("event", ["message_reaction", "message_reaction_count"])
def test_reaction_events_keep_their_fields_without_inventing_message_bodies(event):
    row = message_records()["updates"][0]
    reaction = {
        "chat": {"id": -800, "type": "supergroup", "title": "Synthetic group"},
        "message_id": 900,
        "date": row["data"]["message"]["date"],
    }
    if event == "message_reaction":
        reaction.update(
            {
                "user": {"id": 123, "is_bot": False, "first_name": "Synthetic user"},
                "old_reaction": [],
                "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            }
        )
    else:
        reaction["reactions"] = [{"type": {"type": "emoji", "emoji": "👍"}, "total_count": 2}]
    row["data"] = {"update_id": 321, event: reaction}
    result = migration.transform_update(row, AS_OF)
    assert result["messages"] == []
    assert result["data"] == row["data"]


@pytest.mark.parametrize("options", [{}, {"is_disabled": True}, {"is_disabled": False, "prefer_large_media": False}])
def test_link_preview_library_defaults_do_not_reject_or_change_original_json(options):
    row = message_records()["updates"][0]
    row["data"]["message"]["link_preview_options"] = options
    result = migration.transform_update(row, AS_OF)
    assert result["messages"][0]["data"]["link_preview_options"] == options
    assert result["messages"][0]["data"]["opaque"] == Decimal("0.12345678901234567890123456789")


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

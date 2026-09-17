"""Explicit administrative storage transfer; never imported by the bot or deployment.

Exports are immutable, private recovery artifacts. Source snapshots never write;
imports upsert only source-owned fields and never delete target records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections import Counter, deque
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

FORMAT = 2
MAX_LINE = 32 * 1024 * 1024
IDENTIFIER = re.compile(r"[a-z_][a-z_0-9]*\Z")
TABLES = {
    "users": "telegram::User",
    "chats": "telegram::Chat",
    "directory": "msu_hub::EcosystemChat",
    "vk_subscriptions": "vk_tg::VkWallPosting",
    "updates": "telegram::BotUpdate",
}
# These maps constrain imports, not the introspected source export.
COLUMNS = {
    "users": "id created user_id is_bot first_name last_name username language_code metadata",
    "chats": "id created chat_id type title username first_name last_name metadata",
    "directory": "id created chat_id name section is_hidden username_alias members pinned_message_id",
    "vk_subscriptions": "id created owner_id chat_id last_post_id with_reposts with_header is_suspended description",
    "updates": "id created data handled",
}
SOURCE_TYPES = {
    "id": "std::uuid",
    "created": "std::datetime",
    "metadata": "std::json",
    "data": "std::json",
    **dict.fromkeys(("user_id", "chat_id", "owner_id"), "std::int64"),
    **dict.fromkeys(("members", "pinned_message_id", "last_post_id"), "std::int32"),
    **dict.fromkeys(("is_bot", "is_hidden", "with_reposts", "with_header", "is_suspended", "handled"), "std::bool"),
}
SCHEMA_QUERY = """
SELECT schema::ObjectType {
    name, properties: {name, required, cardinality, target: {name}}, links: {name}
}
FILTER NOT .abstract AND (
    .name LIKE 'telegram::%' OR .name LIKE 'msu_hub::%' OR .name LIKE 'vk_tg::%'
)
ORDER BY .name;
"""


class MigrationError(Exception):
    """An intentionally record-free failure category."""


def utc(value: str | datetime) -> str:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if stamp.tzinfo is None:
        raise MigrationError("naive_timestamp")
    return stamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise MigrationError("duplicate_json_key")
        result[key] = value
    return result


def decode(raw: str) -> Any:
    def invalid(_value: str) -> None:
        raise MigrationError("nonfinite_number")

    return json.loads(raw, parse_float=Decimal, parse_constant=invalid, object_pairs_hook=_pairs)


def decode_object(raw: str) -> dict[str, Any]:
    result = decode(raw)
    if not isinstance(result, dict):
        raise MigrationError("json_object_required")
    return result


def canonical(value: Any) -> str:
    """Canonical JSON without converting arbitrary JSON numbers to binary floats."""
    if value is None or isinstance(value, (str, bool, int)):
        return json.dumps(value, ensure_ascii=True, allow_nan=False)
    if isinstance(value, Decimal):
        if not value.is_finite() or abs(value.adjusted()) > 150_000:
            raise MigrationError("unsupported_number")
        text = format(value, "f")
        text = text.rstrip("0").rstrip(".") if "." in text else text
        return "0" if not value else text
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return "{" + ",".join(canonical(key) + ":" + canonical(value[key]) for key in sorted(value)) + "}"
    raise MigrationError("unsupported_json_value")


def private_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise MigrationError("private_configuration_required")
    value = decode(path.read_text())
    if not isinstance(value, dict):
        raise MigrationError("configuration_shape")
    return value


def save_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        output.write(canonical(value) + "\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def fields(schema: dict[str, Any]) -> list[str]:
    properties = schema["properties"]
    names = sorted(item["name"] for item in properties)
    if len(set(names)) != len(names) or not all(IDENTIFIER.fullmatch(name) for name in names):
        raise MigrationError("unsupported_source_identifier")
    if "id" not in names or "created" not in names:
        raise MigrationError("source_identity_missing")
    if any(item["cardinality"] != "One" for item in properties):
        raise MigrationError("unsupported_source_cardinality")
    if any(item["name"] != "__type__" for item in schema["links"]):
        raise MigrationError("unmapped_source_relationship")
    return names


def normalized_row(row: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if set(row) != set(fields(schema)):
        raise MigrationError("source_field_mismatch")
    for item in schema["properties"]:
        name = item["name"]
        if row[name] is not None and item["target"]["name"] == "std::datetime":
            row[name] = utc(row[name])
    row["id"] = str(UUID(row["id"]))
    return row


def manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(manifest).encode()).hexdigest()


def selection_cutoff(manifest: dict[str, Any]) -> str | None:
    version = manifest.get("format")
    if type(version) is not int or version not in {1, FORMAT}:
        raise MigrationError("manifest_format")
    if version == 1:
        if "selection" in manifest:
            raise MigrationError("legacy_manifest_selection")
        return None
    selection = manifest.get("selection")
    if not isinstance(selection, dict) or set(selection) != {"updates"}:
        raise MigrationError("manifest_selection")
    predicate = selection["updates"]
    if predicate is None:
        return None
    if (
        not isinstance(predicate, dict)
        or set(predicate) != {"field", "operator", "value"}
        or predicate["field"] != "created"
        or predicate["operator"] != ">"
        or not isinstance(predicate["value"], str)
    ):
        raise MigrationError("manifest_selection")
    cutoff = utc(predicate["value"])
    if cutoff != predicate["value"] or cutoff > utc(manifest["snapshot_at"]):
        raise MigrationError("manifest_selection_cutoff")
    return cutoff


def require_retention_coverage(manifest: dict[str, Any], as_of: str) -> None:
    cutoff = selection_cutoff(manifest)
    if cutoff is not None and cutoff > utc(datetime.fromisoformat(utc(as_of)) - timedelta(days=30)):
        raise MigrationError("export_does_not_cover_retention_window")


def selection_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    cutoff = selection_cutoff(manifest)
    entry = manifest["tables"]["updates"]
    return {
        "updates_since": cutoff,
        "source_total": entry.get("source_count", entry["count"]),
        "selected": entry["count"],
        "excluded": entry.get("excluded_count", 0),
    }


class Digests:
    def __init__(self, names: Iterable[str]) -> None:
        self.count = 0
        self.total = hashlib.sha256()
        self.per_field = {name: hashlib.sha256() for name in names}
        self.last_id = ""

    def add(self, row: dict[str, Any]) -> str:
        identifier = row["id"]
        if identifier <= self.last_id:
            raise MigrationError("nonmonotonic_identity")
        self.last_id = identifier
        line = canonical(row) + "\n"
        self.total.update(line.encode())
        for name, digest in self.per_field.items():
            digest.update((canonical([identifier, row[name]]) + "\n").encode())
        self.count += 1
        return line

    def result(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "sha256": self.total.hexdigest(),
            "fields": {name: value.hexdigest() for name, value in self.per_field.items()},
        }


def export_snapshot(source: Any, directory: Path, bot_id: int, batch_size: int, *, updates_since: str | None = None) -> dict[str, Any]:
    """The caller supplies one already-entered read-only transaction."""
    schemas = decode(source.query_json(SCHEMA_QUERY))
    by_type = {item["name"]: item for item in schemas}
    if set(by_type) != set(TABLES.values()):
        raise MigrationError("source_type_mismatch")
    snapshot = utc(source.query_single("SELECT datetime_current();"))
    cutoff = utc(updates_since) if updates_since is not None else None
    if cutoff is not None and cutoff > snapshot:
        raise MigrationError("export_cutoff_after_snapshot")
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "export_id": str(uuid4()),
        "bot_id": bot_id,
        "snapshot_at": snapshot,
        "consistency": "single_readonly_transaction",
        "selection": {"updates": {"field": "created", "operator": ">", "value": cutoff} if cutoff is not None else None},
        "tables": {},
    }
    for table, type_name in TABLES.items():
        schema = by_type[type_name]
        names = fields(schema)
        total = source.query_single(f"SELECT count({type_name});")
        selected_cutoff = cutoff if table == "updates" else None
        selection_values = {"updates_since": datetime.fromisoformat(selected_cutoff)} if selected_cutoff is not None else {}
        predicates = [".created > <datetime>$updates_since"] if selected_cutoff is not None else []
        count = (
            source.query_single(f"SELECT count((SELECT {type_name} FILTER {predicates[0]}));", **selection_values) if predicates else total
        )
        if type(total) is not int or type(count) is not int or not 0 <= count <= total:
            raise MigrationError("source_count_shape")
        print(canonical({"exporting_table": table, "rows": 0, "expected_rows": count}), flush=True)
        if selected_cutoff is not None:
            print(canonical({"source_updates": total, "selected_updates": count, "excluded_updates": total - count}), flush=True)
        digest = Digests(names)
        last_progress = time.monotonic()
        with (directory / (table + ".jsonl")).open("x", encoding="utf-8") as output:
            os.chmod(output.name, 0o600)
            while True:
                conditions = [*predicates, *([".id > <uuid>$after"] if digest.count else [])]
                condition = " FILTER " + " AND ".join(conditions) if conditions else ""
                values: dict[str, Any] = {**selection_values, **({"after": UUID(digest.last_id)} if digest.count else {})}
                raw = source.query_json(
                    f"SELECT {type_name} {{ id }}{condition} ORDER BY .id LIMIT <int64>$limit;",
                    limit=batch_size,
                    **values,
                )
                page = decode(raw)
                if not isinstance(page, list) or len(page) > batch_size:
                    raise MigrationError("source_page_shape")
                identifiers = []
                previous = digest.last_id
                for item in page:
                    if not isinstance(item, dict) or set(item) != {"id"} or not isinstance(item["id"], str):
                        raise MigrationError("source_page_identity")
                    try:
                        identifier = UUID(item["id"])
                    except ValueError:
                        raise MigrationError("source_page_identity") from None
                    if str(identifier) <= previous:
                        raise MigrationError("nonmonotonic_identity")
                    identifiers.append(identifier)
                    previous = str(identifier)
                if not identifiers:
                    break
                # Bound the identity set before asking the source to construct wide JSON shapes.
                raw = source.query_json(
                    f"SELECT {type_name} {{ {', '.join(names)} }} FILTER .id IN array_unpack(<array<uuid>>$ids) ORDER BY .id;",
                    ids=identifiers,
                )
                rows = decode(raw)
                if not isinstance(rows, list) or len(rows) != len(identifiers):
                    raise MigrationError("source_page_record_mismatch")
                normalized = [normalized_row(row, schema) for row in rows]
                if [row["id"] for row in normalized] != [str(identifier) for identifier in identifiers]:
                    raise MigrationError("source_page_record_mismatch")
                if selected_cutoff is not None and any(utc(row["created"]) <= selected_cutoff for row in normalized):
                    raise MigrationError("source_selection_mismatch")
                for row in normalized:
                    output.write(digest.add(row))
                if time.monotonic() - last_progress >= 30:
                    print(canonical({"exporting_table": table, "rows": digest.count, "expected_rows": count}), flush=True)
                    last_progress = time.monotonic()
                if len(identifiers) < batch_size:
                    break
            output.flush()
            os.fsync(output.fileno())
        if count != digest.count:
            raise MigrationError("source_count_mismatch")
        manifest["tables"][table] = {"schema": schema, "source_count": total, "excluded_count": total - count, **digest.result()}
        print(canonical({"exported_table": table, "rows": digest.count}), flush=True)
    return manifest


def export(config: dict[str, Any], directory: Path, bot_id: int, batch_size: int, *, updates_since: str | None = None) -> None:
    import edgedb

    allowed = {"dsn", "host", "port", "database", "user", "password", "tls_security", "tls_ca", "credentials_file"}
    connection = config["connection"]
    if (
        not isinstance(connection, dict)
        or set(connection) - allowed
        or not config.get("expected_database")
        or not any(connection.get(key) for key in ("dsn", "host", "credentials_file"))
    ):
        raise MigrationError("source_configuration")
    directory.mkdir(mode=0o700)
    client = edgedb.create_client(**connection, max_concurrency=1, timeout=15, wait_until_available=15)
    client = client.with_transaction_options(edgedb.TransactionOptions(readonly=True, deferrable=True))
    client = client.with_retry_options(edgedb.RetryOptions(attempts=1))
    try:
        for transaction in client.transaction():
            with transaction:
                if transaction.query_single("SELECT sys::get_current_database();") != config["expected_database"]:
                    raise MigrationError("source_database_mismatch")
                manifest = export_snapshot(transaction, directory, bot_id, batch_size, updates_since=updates_since)
        # No manifest is published until the entire single snapshot has committed.
        save_json(directory / "manifest.json", manifest)
    finally:
        client.close()


def rows(directory: Path, table: str, schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    path = directory / (table + ".jsonl")
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise MigrationError("private_export_required")
    with path.open(encoding="utf-8") as source:
        while line := source.readline(MAX_LINE + 1):
            if len(line) > MAX_LINE or not line.endswith("\n"):
                raise MigrationError("record_size_or_truncation")
            value = decode(line)
            if not isinstance(value, dict):
                raise MigrationError("record_shape")
            yield normalized_row(value, schema)


def verify_export(directory: Path) -> dict[str, Any]:
    manifest = private_json(directory / "manifest.json")
    cutoff = selection_cutoff(manifest)
    if set(manifest.get("tables", {})) != set(TABLES) or manifest.get("consistency") != "single_readonly_transaction":
        raise MigrationError("manifest_format")
    UUID(manifest["export_id"])
    utc(manifest["snapshot_at"])
    if type(manifest["bot_id"]) is not int or not 0 < manifest["bot_id"] < 2**63:
        raise MigrationError("manifest_bot_identity")
    for table, entry in manifest["tables"].items():
        if entry["schema"]["name"] != TABLES[table]:
            raise MigrationError("manifest_type_mismatch")
        if type(entry.get("count")) is not int or entry["count"] < 0:
            raise MigrationError("manifest_count")
        if manifest["format"] == FORMAT:
            if (
                type(entry.get("source_count")) is not int
                or type(entry.get("excluded_count")) is not int
                or not 0 <= entry["count"] <= entry["source_count"]
                or entry["excluded_count"] != entry["source_count"] - entry["count"]
                or ((table != "updates" or cutoff is None) and entry["excluded_count"] != 0)
            ):
                raise MigrationError("manifest_selection_counts")
        elif "source_count" in entry or "excluded_count" in entry:
            raise MigrationError("legacy_manifest_selection")
        digest = Digests(fields(entry["schema"]))
        for row in rows(directory, table, entry["schema"]):
            if table == "updates" and cutoff is not None and utc(row["created"]) <= cutoff:
                raise MigrationError("export_selection_mismatch")
            digest.add(row)
        if any(entry[key] != value for key, value in digest.result().items()):
            raise MigrationError("export_checksum_mismatch")
    return manifest


def copy_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def copy_unescape(text: str) -> str:
    # A PostgreSQL JSON text column never has literal tabs/newlines/control bytes;
    # COPY only adds one escaping layer around its existing JSON backslashes.
    result = []
    index = 0
    while index < len(text):
        if text[index] == "\\":
            if text[index : index + 2] != "\\\\":
                raise MigrationError("unexpected_copy_escape")
            index += 1
        result.append(text[index])
        index += 1
    return "".join(result)


class Postgres:
    def __init__(self, config: dict[str, Any], directory: Path, bot_id: int) -> None:
        self.config = config
        self.directory = directory
        self.bot_id = bot_id
        connection = config["connection"]
        allowed = {"PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSSLMODE", "PGSSLROOTCERT"}
        if not isinstance(connection, dict) or set(connection) - allowed or not all(isinstance(v, str) for v in connection.values()):
            raise MigrationError("target_configuration")
        if config.get("expected_database") != connection.get("PGDATABASE") or not config.get("expected_system_identifier"):
            raise MigrationError("target_identity_required")
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        self.environment.update(connection)
        self.environment["PGCONNECT_TIMEOUT"] = "15"
        self.command = config.get("psql_command", ["psql"])
        if not isinstance(self.command, list) or not self.command or not all(isinstance(v, str) for v in self.command):
            raise MigrationError("target_command")
        if self.command != ["psql"]:
            # Docker may forward named PG environment variables; credential
            # values and arbitrary executable arguments never belong in argv.
            prefix = self.command[:-2]
            if (
                len(self.command) < 7
                or self.command[:3] != ["docker", "exec", "-i"]
                or self.command[-1] != "psql"
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", self.command[-2])
                or len(prefix[3:]) % 2
                or any(prefix[i] != "-e" or prefix[i + 1] not in allowed for i in range(3, len(prefix), 2))
            ):
                raise MigrationError("target_command_not_allowlisted")
        self.command = [*self.command, "-X", "-qAt", "-v", "ON_ERROR_STOP=1"]
        self.fingerprint = hashlib.sha256(
            canonical(
                {
                    "database": config["expected_database"],
                    "cluster": config["expected_system_identifier"],
                    "bot_id": bot_id,
                }
            ).encode()
        ).hexdigest()

    def run(self, script: str, *, timeout: int = 300) -> str:
        with (self.directory / "target-diagnostics.log").open("ab") as diagnostics:
            os.chmod(diagnostics.name, 0o600)
            result = subprocess.run(
                self.command, input=script, text=True, stdout=subprocess.PIPE, stderr=diagnostics, env=self.environment, timeout=timeout
            )
        if result.returncode:
            raise MigrationError("target_operation_failed")
        return result.stdout

    def run_file(self, path: Path) -> str:
        with path.open() as source, (self.directory / "target-diagnostics.log").open("ab") as diagnostics:
            result = subprocess.run(
                self.command, stdin=source, text=True, stdout=subprocess.PIPE, stderr=diagnostics, env=self.environment, timeout=600
            )
        if result.returncode:
            raise MigrationError("target_audit_failed")
        return result.stdout

    def guard(self) -> None:
        result = decode(
            self.run(f"""
SELECT json_build_object('database',current_database(),'cluster',system_identifier::text,
  'version',(SELECT max(version) FROM msu_hub_private.schema_migrations),
  'principal',EXISTS(SELECT 1 FROM msu_hub_private.principals WHERE enabled AND bot_id={self.bot_id}))
FROM pg_control_system();
""")
        )
        if result != {
            "database": self.config["expected_database"],
            "cluster": self.config["expected_system_identifier"],
            "version": self.config.get("expected_schema_version", 3),
            "principal": True,
        }:
            raise MigrationError("target_identity_mismatch")

    def stream(self, query: str) -> Iterator[dict[str, Any]]:
        # COPY streams through libpq instead of materializing the full result in psql.
        with (self.directory / "target-diagnostics.log").open("ab") as diagnostics:
            process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostics, env=self.environment, text=True
            )
            try:
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nCOPY (" + query + ") TO STDOUT;\nCOMMIT;\n")
                process.stdin.close()
                while line := process.stdout.readline(MAX_LINE + 1):
                    if len(line) > MAX_LINE or not line.endswith("\n"):
                        raise MigrationError("target_record_size_or_truncation")
                    yield decode(copy_unescape(line[:-1]))
                if process.wait(timeout=30):
                    raise MigrationError("target_export_failed")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def import_fields(table: str, schema: dict[str, Any]) -> list[str]:
    names = fields(schema)
    expected = set(COLUMNS[table].split())
    if table in {"users", "chats"}:
        expected.add("full_name")
    if set(names) != expected:
        raise MigrationError("unmapped_source_fields")
    if any(item["target"]["name"] != SOURCE_TYPES.get(item["name"], "std::str") for item in schema["properties"]):
        raise MigrationError("unmapped_source_scalar_type")
    return sorted(set(names) - {"full_name"})


def batch_script(table: str, batch: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
    names = import_fields(table, manifest["tables"][table]["schema"])
    columns = ",".join(names)
    # jsonb_populate_record maps JSON null to SQL NULL. Required JSON columns
    # must instead retain the JSON null value itself, including scalar archives.
    projection = ",".join("payload->'" + name + "'" if name in {"metadata", "data"} else "record." + name for name in names)
    assignments = ",".join(name + "=EXCLUDED." + name for name in names if name != "id")
    suffix = ""
    if table in {"users", "chats"}:
        columns += ",first_seen_at,last_seen_at,profile"
        profile = "(payload - ARRAY['id','created','metadata','full_name']::text[])"
        identity = "user_id" if table == "users" else "chat_id"
        profile += f" - '{identity}' || jsonb_build_object('id',record.{identity})"
        projection += f",record.created,'{utc(manifest['snapshot_at'])}'::timestamptz,{profile}"
        assignments += ",first_seen_at=least(existing.first_seen_at,EXCLUDED.first_seen_at),last_seen_at=EXCLUDED.last_seen_at,profile=EXCLUDED.profile"
        if table == "chats":
            suffix = """
INSERT INTO msu_hub_private.chat_settings(chat_id,settings,updated_at)
SELECT (payload->>'chat_id')::bigint,
 CASE WHEN jsonb_typeof(payload->'metadata'->'settings')='object' THEN payload->'metadata'->'settings' ELSE '{}'::jsonb END,
 now() FROM migration_batch
ON CONFLICT(chat_id) DO UPDATE SET settings=EXCLUDED.settings,updated_at=EXCLUDED.updated_at;
"""
    elif table == "updates":
        columns += ",bot_id,update_id,kind,is_legacy"
        projection += f",{manifest['bot_id']},NULL,NULL,true"
        # An existing non-legacy or another bot's UUID must never be overwritten.
        suffix = ""
    checks = ""
    if table == "updates":
        checks = f"""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM migration_batch b JOIN msu_hub_private.updates u ON u.id=(b.payload->>'id')::uuid
           WHERE NOT u.is_legacy OR u.bot_id<>{manifest["bot_id"]}) THEN
   RAISE EXCEPTION 'Migration receipt identity conflict';
 END IF;
END $$;
"""
    copied = "".join(copy_escape(canonical(row)) + "\n" for row in batch)
    return f"""BEGIN;
SET LOCAL statement_timeout='240s';
SELECT pg_advisory_xact_lock({manifest["bot_id"]});
CREATE TEMP TABLE migration_batch(payload jsonb) ON COMMIT DROP;
COPY migration_batch(payload) FROM STDIN;
{copied}\\.
{checks}
INSERT INTO msu_hub_private.{table} AS existing({columns})
SELECT {projection} FROM migration_batch
CROSS JOIN LATERAL jsonb_populate_record(NULL::msu_hub_private.{table},payload) AS record
ON CONFLICT(id) DO UPDATE SET {assignments};
{suffix}COMMIT;
"""


def batches(values: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def import_data(target: Postgres, directory: Path, manifest: dict[str, Any], batch_size: int, *, replay: bool = False) -> None:
    if any(directory.glob("normalization-*/applied-" + target.fingerprint + ".json")):
        raise MigrationError("normalized_target_requires_new_export")
    for table, entry in manifest["tables"].items():
        import_fields(table, entry["schema"])
    target.guard()
    checkpoint = directory / ("import-" + target.fingerprint + ".json")
    identity = {"manifest": manifest_digest(manifest), "batch_size": batch_size}
    state: dict[str, Any] = private_json(checkpoint) if checkpoint.exists() and not replay else {**identity, "completed": {}}
    if any(state.get(key) != value for key, value in identity.items()):
        raise MigrationError("resume_manifest_mismatch")
    for table in TABLES:
        source = rows(directory, table, manifest["tables"][table]["schema"])
        completed = state["completed"].get(table, 0)
        for number, batch in enumerate(batches(source, batch_size), 1):
            if number <= completed:
                continue
            target.run(batch_script(table, batch, manifest))
            # A lost commit acknowledgement leaves no receipt: replaying this
            # absolute upsert is safe and cannot duplicate source identities.
            state["completed"][table] = number
            save_json(checkpoint, state)
        print(canonical({"imported_table": table, "source_rows": manifest["tables"][table]["count"]}), flush=True)


def target_query(table: str, schema: dict[str, Any], bot_id: int) -> str:
    import_fields(table, schema)
    expressions: list[str] = []
    for name in fields(schema):
        if name == "full_name":
            expression = "CASE WHEN last_name IS NULL THEN first_name ELSE first_name || ' ' || last_name END"
            if table == "chats":
                expression = "COALESCE(title," + expression + ")"
        else:
            expression = name
        expressions.extend(("'" + name + "'", expression))
    condition = f" WHERE bot_id={bot_id} AND is_legacy" if table == "updates" else ""
    return f"SELECT jsonb_build_object({','.join(expressions)})::text FROM msu_hub_private.{table}{condition} ORDER BY id"


def compare_rows(expected: Iterable[dict[str, Any]], actual: Iterable[dict[str, Any]], schema: dict[str, Any]) -> dict[str, Any]:
    names = fields(schema)
    source = iter(expected)
    target = iter(actual)
    left, right = next(source, None), next(target, None)
    counts: Counter[str] = Counter()
    mismatch: Counter[str] = Counter()
    expected_digest, actual_digest = Digests(names), Digests(names)
    while left is not None or right is not None:
        if right is None or (left is not None and left["id"] < right["id"]):
            counts["missing"] += 1
            left = next(source, None)
        elif left is None or right["id"] < left["id"]:
            counts["extra"] += 1
            right = next(target, None)
        else:
            normalized_row(right, schema)
            expected_digest.add(left)
            actual_digest.add(right)
            counts["matched"] += 1
            for name in names:
                if canonical(left[name]) != canonical(right[name]):
                    mismatch[name] += 1
            left, right = next(source, None), next(target, None)
    return {
        "matched": counts["matched"],
        "missing": counts["missing"],
        "extra": counts["extra"],
        "field_mismatches": dict(mismatch),
        "expected": expected_digest.result(),
        "actual": actual_digest.result(),
    }


def reconcile(
    target: Postgres,
    directory: Path,
    manifest: dict[str, Any],
    *,
    normalized: bool = False,
    as_of: str | None = None,
    retained_only: bool = False,
) -> dict[str, Any]:
    if normalized or retained_only:
        if as_of is None:
            raise MigrationError("fixed_retention_instant_required")
        require_retention_coverage(manifest, as_of)
    target.guard()
    report: dict[str, Any] = {
        "export_id": manifest["export_id"],
        "export_manifest_sha256": manifest_digest(manifest),
        "selection": selection_summary(manifest),
        "normalized": normalized,
        "retained_only": retained_only,
        "as_of": utc(as_of) if as_of is not None else None,
        "tables": {},
    }
    cutoff = utc(datetime.fromisoformat(utc(as_of)) - timedelta(days=30)) if as_of else None

    def expected_rows(table: str, schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
        from msu_hub_bot.storage.observations import reference_payload

        for row in rows(directory, table, schema):
            if table == "updates":
                if retained_only and cutoff is not None and utc(row["created"]) <= cutoff:
                    continue
                if normalized:
                    row["data"] = reference_payload(row["data"])
            yield row

    for table, entry in manifest["tables"].items():
        report["tables"][table] = compare_rows(
            expected_rows(table, entry["schema"]),
            # Expired target receipts must be reported as extras, including
            # after retention; a target-side cutoff would hide incomplete work.
            target.stream(target_query(table, entry["schema"], manifest["bot_id"])),
            entry["schema"],
        )
    report["exact"] = all(not item["missing"] and not item["extra"] and not item["field_mismatches"] for item in report["tables"].values())
    report["source_parity"] = all(not item["missing"] and not item["field_mismatches"] for item in report["tables"].values())
    report["validated"] = report["exact"]
    if normalized and as_of is not None:
        report["derived_entities"] = audit_entities(target, directory, manifest, as_of)
        entities = report["derived_entities"]
        report["validated"] = (
            report["source_parity"]
            and not entities["unexplained_extra_entities"]
            and not entities["missing_observed_entities"]
            and entities["derived_users"] == report["tables"]["users"]["extra"]
            and entities["derived_chats"] == report["tables"]["chats"]["extra"]
            and not any(report["tables"][table]["extra"] for table in ("directory", "vk_subscriptions", "updates"))
        )
    suffix = "-normalized" if normalized else ""
    save_json(directory / ("reconciliation-" + target.fingerprint + suffix + ".json"), report)
    return report


def transform_update(row: dict[str, Any], as_of: str) -> dict[str, Any]:
    """Typed identities plus original JSON bodies; never round opaque JSON numbers."""
    from aiogram.types import Update

    from msu_hub_bot.storage.observations import archive_observation, is_message_payload, reference_payload

    raw = row["data"]
    if not isinstance(raw, dict) or type(raw.get("update_id")) is not int:
        raise MigrationError("legacy_update_shape")
    archive = archive_observation(
        # The typed copy discovers identities/relationships only. Every stored
        # receipt/message body below comes from the original Decimal-valued tree.
        Update.model_validate_json(canonical(raw)),
        row["handled"],
        received_at=datetime.fromisoformat(utc(row["created"])),
        retention_at=datetime.fromisoformat(utc(as_of)),
    )
    archive.id = UUID(row["id"])
    result = decode_object(archive.model_dump_json(exclude_unset=True))
    result["data"] = reference_payload(raw)
    message_sources: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    pending: deque[Any] = deque([raw])
    seen = 0
    while pending:
        value = pending.popleft()
        seen += 1
        if seen > 4096:
            raise MigrationError("legacy_traversal_limit")
        if isinstance(value, dict):
            chat = value.get("chat")
            if is_message_payload(value) and isinstance(chat, dict):
                key = (value.get("business_connection_id") or "", chat.get("id"), value["message_id"])
                message_sources.setdefault(key, value)
            pending.extend(item for item in value.values() if isinstance(item, (dict, list)))
        elif isinstance(value, list):
            pending.extend(item for item in value if isinstance(item, (dict, list)))
    for message in result["messages"]:
        key = (message["business_connection_id"], message["chat_id"], message["message_id"])
        if key not in message_sources:
            raise MigrationError("legacy_message_source_missing")
        message["data"] = reference_payload(message_sources[key], message_body=True)
    extracted = {(item["business_connection_id"], item["chat_id"], item["message_id"]) for item in result["messages"]}
    cutoff = datetime.fromisoformat(utc(as_of)) - timedelta(days=30)

    def eligible(value: Any) -> bool:
        if isinstance(value, str):
            return utc(value) > utc(cutoff)
        if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            return value > Decimal(str(cutoff.timestamp()))
        raise MigrationError("legacy_message_date")

    if any(key not in extracted and eligible(value["date"]) for key, value in message_sources.items()):
        raise MigrationError("eligible_legacy_message_not_extracted")
    return result


def normalization_path(directory: Path, as_of: str) -> Path:
    return directory / ("normalization-" + hashlib.sha256(utc(as_of).encode()).hexdigest()[:16])


def json_lines(path: Path) -> Iterator[dict[str, Any]]:
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise MigrationError("private_artifact_required")
    with path.open(encoding="utf-8") as source:
        while line := source.readline(MAX_LINE + 1):
            if len(line) > MAX_LINE or not line.endswith("\n"):
                raise MigrationError("artifact_size_or_truncation")
            value = decode(line)
            if not isinstance(value, dict):
                raise MigrationError("artifact_shape")
            yield value


def prepare_normalization(directory: Path, manifest: dict[str, Any], as_of: str) -> dict[str, Any]:
    require_retention_coverage(manifest, as_of)
    destination = normalization_path(directory, as_of)
    destination.mkdir(mode=0o700)
    digest = hashlib.sha256()
    counts: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    cutoff = utc(datetime.fromisoformat(utc(as_of)) - timedelta(days=30))
    last_progress = time.monotonic()
    with (destination / "records.jsonl").open("x") as output, (destination / "rejects.jsonl").open("x") as rejects:
        os.chmod(output.name, 0o600)
        os.chmod(rejects.name, 0o600)
        for row in rows(directory, "updates", manifest["tables"]["updates"]["schema"]):
            counts["source_rows"] += 1
            counts["expired_receipts" if utc(row["created"]) <= cutoff else "retained_receipts"] += 1
            try:
                transformed = transform_update(row, as_of)
                line = canonical(transformed) + "\n"
            except Exception as error:
                # Malformed historical records stay in the exact export/target;
                # all transformation failures are visible and block application.
                category = str(error) if isinstance(error, MigrationError) else type(error).__name__
                categories[category] += 1
                rejects.write(canonical({"category": category, "source": row}) + "\n")
                continue
            output.write(line)
            digest.update(line.encode())
            counts["accepted"] += 1
            counts["message_observations"] += len(transformed["messages"])
            if time.monotonic() - last_progress >= 30:
                print(canonical({"normalizing_rows": counts["source_rows"], "rejected": sum(categories.values())}), flush=True)
                last_progress = time.monotonic()
        for file in (output, rejects):
            file.flush()
            os.fsync(file.fileno())
    report = {
        "format": FORMAT,
        "export_id": manifest["export_id"],
        "export_manifest_sha256": manifest_digest(manifest),
        "selection": selection_summary(manifest),
        "as_of": utc(as_of),
        "cutoff": cutoff,
        "sha256": digest.hexdigest(),
        "counts": dict(counts),
        "rejections": dict(categories),
    }
    save_json(destination / "manifest.json", report)
    return report


def verify_normalization(directory: Path, manifest: dict[str, Any], as_of: str) -> tuple[Path, dict[str, Any]]:
    require_retention_coverage(manifest, as_of)
    destination = normalization_path(directory, as_of)
    report = private_json(destination / "manifest.json")
    if (
        type(report.get("format")) is not int
        or report["format"] not in {1, FORMAT}
        or report.get("export_id") != manifest["export_id"]
        or report.get("as_of") != utc(as_of)
        or (report["format"] == 1 and manifest["format"] != 1)
        or (report["format"] == FORMAT and report.get("export_manifest_sha256") != manifest_digest(manifest))
    ):
        raise MigrationError("normalization_manifest_mismatch")
    digest = hashlib.sha256()
    count = 0
    for row in json_lines(destination / "records.jsonl"):
        digest.update((canonical(row) + "\n").encode())
        count += 1
    if digest.hexdigest() != report["sha256"] or count != report["counts"].get("accepted", 0):
        raise MigrationError("normalization_checksum_mismatch")
    if report["rejections"] or count != manifest["tables"]["updates"]["count"]:
        raise MigrationError("normalization_requires_rejection_review")
    return destination, report


def normalization_script(batch: list[dict[str, Any]], bot_id: int, as_of: str) -> str:
    copied = "".join(copy_escape(canonical(row)) + "\n" for row in batch)
    return f"""BEGIN;
SET LOCAL statement_timeout='240s';
SELECT pg_advisory_xact_lock({bot_id});
CREATE TEMP TABLE migration_batch(payload jsonb) ON COMMIT DROP;
COPY migration_batch(payload) FROM STDIN;
{copied}\\.
DO $$ DECLARE item jsonb; BEGIN
 FOR item IN SELECT payload FROM migration_batch LOOP
  IF NOT EXISTS(SELECT 1 FROM msu_hub_private.updates WHERE id=(item->>'id')::uuid AND bot_id={bot_id}
                AND is_legacy AND created=(item->>'received_at')::timestamptz AND handled=(item->>'handled')::boolean) THEN
   RAISE EXCEPTION 'Legacy receipt does not match verified source';
  END IF;
  PERFORM msu_hub_private.observe_archive(item,{bot_id},(item->>'id')::uuid,(item->>'received_at')::timestamptz,'{utc(as_of)}'::timestamptz);
  UPDATE msu_hub_private.updates SET data=item->'data',update_id=(item->>'update_id')::bigint,kind=item->>'kind'
   WHERE id=(item->>'id')::uuid;
 END LOOP;
END $$;
COMMIT;
"""


def normalize(target: Postgres, directory: Path, manifest: dict[str, Any], as_of: str, batch_size: int) -> dict[str, Any]:
    destination, report = verify_normalization(directory, manifest, as_of)
    parity = private_json(directory / ("reconciliation-" + target.fingerprint + ".json"))
    if (
        parity.get("export_id") != manifest["export_id"]
        or not parity.get("exact")
        or parity.get("normalized", False)
        or parity.get("retained_only", False)
        or (
            (manifest["format"] == FORMAT or "export_manifest_sha256" in parity)
            and parity.get("export_manifest_sha256") != manifest_digest(manifest)
        )
    ):
        raise MigrationError("verified_raw_parity_required")
    target.guard()
    checkpoint = destination / ("applied-" + target.fingerprint + ".json")
    identity: dict[str, Any] = {"batch_size": batch_size, "sha256": report["sha256"]}
    if report["format"] == FORMAT:
        identity["manifest"] = manifest_digest(manifest)
    state = private_json(checkpoint) if checkpoint.exists() else {"completed": 0, **identity}
    if any(state.get(key) != value for key, value in identity.items()):
        raise MigrationError("normalization_resume_mismatch")
    completed = state["completed"]
    for number, batch in enumerate(batches(json_lines(destination / "records.jsonl"), batch_size), 1):
        if number > completed:
            target.run(normalization_script(batch, manifest["bot_id"], as_of))
            state["completed"] = number
            save_json(checkpoint, state)
    return report


def retention_report(target: Postgres, as_of: str) -> dict[str, Any]:
    target.guard()
    return decode_object(
        target.run(f"""
SELECT jsonb_build_object('as_of','{utc(as_of)}','expired_updates',
 (SELECT count(*) FROM msu_hub_private.updates WHERE bot_id={target.bot_id} AND created<='{utc(as_of)}'::timestamptz-interval '30 days'),
 'retained_updates',(SELECT count(*) FROM msu_hub_private.updates WHERE bot_id={target.bot_id} AND created>'{utc(as_of)}'::timestamptz-interval '30 days'),
 'expired_messages',(SELECT count(*) FROM msu_hub_private.messages WHERE bot_id={target.bot_id} AND sent_at<='{utc(as_of)}'::timestamptz-interval '30 days'),
 'retained_messages',(SELECT count(*) FROM msu_hub_private.messages WHERE bot_id={target.bot_id} AND sent_at>'{utc(as_of)}'::timestamptz-interval '30 days'));
""")
    )


def audit_messages(target: Postgres, directory: Path, manifest: dict[str, Any], as_of: str) -> dict[str, Any]:
    """Compare keys, winning versions and exact JSON body hashes inside PostgreSQL."""
    destination, report = verify_normalization(directory, manifest, as_of)
    target.guard()
    path = destination / "message-audit.sql"
    with path.open("w") as output:
        os.chmod(path, 0o600)
        output.write(
            "BEGIN ISOLATION LEVEL REPEATABLE READ;\nSET LOCAL statement_timeout='540s';\n"
            "CREATE TEMP TABLE migration_expected(payload jsonb) ON COMMIT DROP;\n"
            "COPY migration_expected(payload) FROM STDIN;\n"
        )
        for receipt in json_lines(destination / "records.jsonl"):
            for message in receipt["messages"]:
                payload = {**message, "bot_id": manifest["bot_id"], "source_update_id": receipt["id"]}
                output.write(copy_escape(canonical(payload)) + "\n")
        output.write(f"""\\.
WITH typed AS (SELECT r.* FROM migration_expected
 CROSS JOIN LATERAL jsonb_populate_record(NULL::msu_hub_private.messages,payload) AS r), ranked AS (
 SELECT *,min(sent_at) OVER(PARTITION BY bot_id,chat_id,message_id,business_connection_id) AS earliest,
 row_number() OVER(PARTITION BY bot_id,chat_id,message_id,business_connection_id
 ORDER BY coalesce(edited_at,sent_at) DESC,observed_at DESC,source_update_id DESC) AS position FROM typed), expected AS (
 SELECT (to_jsonb(r)-ARRAY['position','earliest']::text[]) || jsonb_build_object('sent_at',earliest) AS value,
 bot_id,chat_id,message_id,business_connection_id,data FROM ranked r WHERE position=1), actual AS (
 SELECT * FROM msu_hub_private.messages WHERE bot_id={target.bot_id} AND sent_at>'{report["cutoff"]}'::timestamptz)
SELECT jsonb_build_object('expected',count(e.bot_id),'actual',count(a.bot_id),
 'missing',count(*) FILTER(WHERE a.bot_id IS NULL),'extra',count(*) FILTER(WHERE e.bot_id IS NULL),
 'key_version_body_mismatches',count(*) FILTER(WHERE e.bot_id IS NOT NULL AND a.bot_id IS NOT NULL AND e.value<>to_jsonb(a)),
 'body_sha256_mismatches',count(*) FILTER(WHERE e.bot_id IS NOT NULL AND a.bot_id IS NOT NULL
 AND sha256(convert_to(e.data::text,'UTF8'))<>sha256(convert_to(a.data::text,'UTF8'))))
FROM expected e FULL JOIN actual a USING(bot_id,chat_id,message_id,business_connection_id);
ROLLBACK;
""")
    result = decode_object(target.run_file(path))
    save_json(destination / ("message-audit-" + target.fingerprint + ".json"), result)
    return result


def audit_entities(target: Postgres, directory: Path, manifest: dict[str, Any], as_of: str) -> dict[str, Any]:
    """Account for newly discovered identities against verified observations."""
    destination, _ = verify_normalization(directory, manifest, as_of)
    path = destination / "entity-audit.sql"
    with path.open("w") as output:
        os.chmod(path, 0o600)
        output.write(
            "BEGIN ISOLATION LEVEL REPEATABLE READ;\nSET LOCAL statement_timeout='540s';\n"
            "CREATE TEMP TABLE migration_original(kind text,id uuid,telegram_id bigint) ON COMMIT DROP;\n"
            "COPY migration_original FROM STDIN;\n"
        )
        for table, identity in (("users", "user_id"), ("chats", "chat_id")):
            for row in rows(directory, table, manifest["tables"][table]["schema"]):
                output.write(f"{table}\t{UUID(row['id'])}\t{int(row[identity])}\n")
        output.write(
            "\\.\nCREATE TEMP TABLE migration_observed(kind text,telegram_id bigint) ON COMMIT DROP;\nCOPY migration_observed FROM STDIN;\n"
        )
        for receipt in json_lines(destination / "records.jsonl"):
            for table, identity in (("users", "user_id"), ("chats", "chat_id")):
                for row in receipt[table]:
                    output.write(f"{table}\t{int(row[identity])}\n")
        output.write("""\\.
WITH observed AS (SELECT DISTINCT kind,telegram_id FROM migration_observed), current AS (
 SELECT 'users' AS kind,id,user_id AS telegram_id FROM msu_hub_private.users UNION ALL
 SELECT 'chats',id,chat_id FROM msu_hub_private.chats), extra AS (
 SELECT c.* FROM current c LEFT JOIN migration_original o USING(kind,id) WHERE o.id IS NULL)
SELECT jsonb_build_object('derived_users',(SELECT count(*) FROM extra WHERE kind='users'),
 'derived_chats',(SELECT count(*) FROM extra WHERE kind='chats'),
 'unexplained_extra_entities',(SELECT count(*) FROM extra e LEFT JOIN observed o USING(kind,telegram_id) WHERE o.telegram_id IS NULL),
 'missing_observed_entities',(SELECT count(*) FROM observed o LEFT JOIN current c USING(kind,telegram_id) WHERE c.id IS NULL));
ROLLBACK;
""")
    return decode_object(target.run_file(path))


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("export", "verify", "import", "reconcile", "prepare-normalization", "normalize", "audit-messages", "retention-report"),
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--target-config", type=Path)
    parser.add_argument("--bot-id", type=int)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--replay", action="store_true", help="Reapply acknowledged import batches; never delete rows")
    parser.add_argument("--as-of", help="Fixed timezone-aware retention instant for preparation and validation")
    parser.add_argument("--updates-since", help="Export only receipts created strictly after this fixed timezone-aware instant")
    parser.add_argument("--normalized", action="store_true", help="Compare receipt references after successful normalization")
    parser.add_argument("--retained-only", action="store_true", help="Compare only receipts newer than the fixed 30-day cutoff")
    parser.add_argument("--target-writers-stopped", action="store_true", help="Operator assertion that application writers are stopped")
    args = parser.parse_args()
    try:
        if not 1 <= args.batch_size <= 1000:
            raise MigrationError("batch_size_range")
        if args.updates_since is not None:
            if args.action != "export":
                raise MigrationError("updates_since_requires_export")
            utc(args.updates_since)
        if args.action in {"import", "normalize", "reconcile"} and not args.target_writers_stopped:
            raise MigrationError("target_writer_freeze_required")
        if (
            args.action in {"prepare-normalization", "normalize", "audit-messages", "retention-report"}
            or args.retained_only
            or args.normalized
        ) and not args.as_of:
            raise MigrationError("fixed_retention_instant_required")
        if args.as_of:
            utc(args.as_of)
        if args.action == "export":
            if args.source_config is None or args.bot_id is None or not 0 < args.bot_id < 2**63:
                raise MigrationError("source_and_bot_required")
            export(private_json(args.source_config), args.directory, args.bot_id, args.batch_size, updates_since=args.updates_since)
        else:
            manifest = verify_export(args.directory)
            if args.as_of:
                require_retention_coverage(manifest, args.as_of)
            if args.action == "prepare-normalization":
                report = prepare_normalization(args.directory, manifest, args.as_of)
                print(
                    canonical({"normalization": report["counts"], "selection": report["selection"], "rejections": report["rejections"]}),
                    flush=True,
                )
                if report["rejections"]:
                    return 2
            elif args.action != "verify":
                if args.target_config is None:
                    raise MigrationError("target_required")
                target = Postgres(private_json(args.target_config), args.directory, manifest["bot_id"])
                if args.action == "import":
                    import_data(target, args.directory, manifest, args.batch_size, replay=args.replay)
                elif args.action == "normalize":
                    report = normalize(target, args.directory, manifest, args.as_of, args.batch_size)
                    print(canonical({"normalized": report["counts"], "selection": selection_summary(manifest)}), flush=True)
                elif args.action == "retention-report":
                    print(canonical(retention_report(target, args.as_of)), flush=True)
                elif args.action == "audit-messages":
                    report = audit_messages(target, args.directory, manifest, args.as_of)
                    print(canonical(report), flush=True)
                    if any(report[key] for key in ("missing", "extra", "key_version_body_mismatches", "body_sha256_mismatches")):
                        return 2
                else:
                    report = reconcile(
                        target, args.directory, manifest, normalized=args.normalized, as_of=args.as_of, retained_only=args.retained_only
                    )
                    print(
                        canonical(
                            {
                                "reconciled": report["validated"],
                                "selection": report["selection"],
                                "derived_entities": report.get("derived_entities", {}),
                                "tables": {
                                    table: {key: value for key, value in item.items() if key not in {"expected", "actual"}}
                                    for table, item in report["tables"].items()
                                },
                            }
                        ),
                        flush=True,
                    )
                    if not report["validated"]:
                        return 2
        print(canonical({"complete": True, "action": args.action}), flush=True)
        return 0
    except Exception as error:
        category = str(error) if isinstance(error, MigrationError) else type(error).__name__
        print(canonical({"complete": False, "category": category}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())

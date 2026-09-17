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
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

FORMAT = 1
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


def export_snapshot(source: Any, directory: Path, bot_id: int, batch_size: int) -> dict[str, Any]:
    """The caller supplies one already-entered read-only transaction."""
    schemas = decode(source.query_json(SCHEMA_QUERY))
    by_type = {item["name"]: item for item in schemas}
    if set(by_type) != set(TABLES.values()):
        raise MigrationError("source_type_mismatch")
    snapshot = utc(source.query_single("SELECT datetime_current();"))
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "export_id": str(uuid4()),
        "bot_id": bot_id,
        "snapshot_at": snapshot,
        "consistency": "single_readonly_transaction",
        "tables": {},
    }
    for table, type_name in TABLES.items():
        schema = by_type[type_name]
        names = fields(schema)
        count = source.query_single(f"SELECT count({type_name});")
        digest = Digests(names)
        with (directory / (table + ".jsonl")).open("x", encoding="utf-8") as output:
            os.chmod(output.name, 0o600)
            while True:
                condition = " FILTER .id > <uuid>$after" if digest.count else ""
                values = {"after": UUID(digest.last_id)} if digest.count else {}
                raw = source.query_json(
                    f"SELECT {type_name} {{ {', '.join(names)} }}{condition} ORDER BY .id LIMIT <int64>$limit;",
                    limit=batch_size,
                    **values,
                )
                rows = decode(raw)
                if not isinstance(rows, list) or len(rows) > batch_size:
                    raise MigrationError("source_page_shape")
                for row in rows:
                    output.write(digest.add(normalized_row(row, schema)))
                if len(rows) < batch_size:
                    break
            output.flush()
            os.fsync(output.fileno())
        if count != digest.count:
            raise MigrationError("source_count_mismatch")
        manifest["tables"][table] = {"schema": schema, **digest.result()}
        print(canonical({"exported_table": table, "rows": digest.count}), flush=True)
    return manifest


def export(config: dict[str, Any], directory: Path, bot_id: int, batch_size: int) -> None:
    import edgedb

    allowed = {"dsn", "host", "port", "database", "user", "password", "tls_security", "tls_ca", "credentials_file"}
    connection = config["connection"]
    if not isinstance(connection, dict) or set(connection) - allowed or not config.get("expected_database"):
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
                manifest = export_snapshot(transaction, directory, bot_id, batch_size)
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
    if manifest.get("format") != FORMAT or set(manifest.get("tables", {})) != set(TABLES):
        raise MigrationError("manifest_format")
    UUID(manifest["export_id"])
    utc(manifest["snapshot_at"])
    if type(manifest["bot_id"]) is not int or not 0 < manifest["bot_id"] < 2**63:
        raise MigrationError("manifest_bot_identity")
    for table, entry in manifest["tables"].items():
        if entry["schema"]["name"] != TABLES[table]:
            raise MigrationError("manifest_type_mismatch")
        digest = Digests(fields(entry["schema"]))
        for row in rows(directory, table, entry["schema"]):
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

    def guard(self) -> None:
        result = decode(
            self.run(f"""
SELECT json_build_object('database',current_database(),'cluster',system_identifier::text,
  'version',(SELECT max(version) FROM hub_private.schema_migrations),
  'principal',EXISTS(SELECT 1 FROM hub_private.principals WHERE enabled AND bot_id={self.bot_id}))
FROM pg_control_system();
""")
        )
        if result != {
            "database": self.config["expected_database"],
            "cluster": self.config["expected_system_identifier"],
            "version": 1,
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
INSERT INTO hub_private.chat_settings(chat_id,settings,updated_at)
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
 IF EXISTS(SELECT 1 FROM migration_batch b JOIN hub_private.updates u ON u.id=(b.payload->>'id')::uuid
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
INSERT INTO hub_private.{table} AS existing({columns})
SELECT {projection} FROM migration_batch
CROSS JOIN LATERAL jsonb_populate_record(NULL::hub_private.{table},payload) AS record
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
    for table, entry in manifest["tables"].items():
        import_fields(table, entry["schema"])
    target.guard()
    checkpoint = directory / ("import-" + target.fingerprint + ".json")
    identity = {"manifest": hashlib.sha256(canonical(manifest).encode()).hexdigest(), "batch_size": batch_size}
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
    return f"SELECT jsonb_build_object({','.join(expressions)})::text FROM hub_private.{table}{condition} ORDER BY id"


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


def reconcile(target: Postgres, directory: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    target.guard()
    report: dict[str, Any] = {"export_id": manifest["export_id"], "tables": {}}
    for table, entry in manifest["tables"].items():
        report["tables"][table] = compare_rows(
            rows(directory, table, entry["schema"]),
            target.stream(target_query(table, entry["schema"], manifest["bot_id"])),
            entry["schema"],
        )
    report["exact"] = all(not item["missing"] and not item["extra"] and not item["field_mismatches"] for item in report["tables"].values())
    save_json(directory / ("reconciliation-" + target.fingerprint + ".json"), report)
    return report


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "verify", "import", "reconcile"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--target-config", type=Path)
    parser.add_argument("--bot-id", type=int)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--replay", action="store_true", help="Reapply acknowledged import batches; never delete rows")
    args = parser.parse_args()
    try:
        if not 1 <= args.batch_size <= 1000:
            raise MigrationError("batch_size_range")
        if args.action == "export":
            if args.source_config is None or args.bot_id is None or not 0 < args.bot_id < 2**63:
                raise MigrationError("source_and_bot_required")
            export(private_json(args.source_config), args.directory, args.bot_id, args.batch_size)
        else:
            manifest = verify_export(args.directory)
            if args.action != "verify":
                if args.target_config is None:
                    raise MigrationError("target_required")
                target = Postgres(private_json(args.target_config), args.directory, manifest["bot_id"])
                if args.action == "import":
                    import_data(target, args.directory, manifest, args.batch_size, replay=args.replay)
                else:
                    report = reconcile(target, args.directory, manifest)
                    print(
                        canonical(
                            {
                                "reconciled": report["exact"],
                                "tables": {
                                    table: {key: value for key, value in item.items() if key not in {"expected", "actual"}}
                                    for table, item in report["tables"].items()
                                },
                            }
                        ),
                        flush=True,
                    )
                    if not report["exact"]:
                        return 2
        print(canonical({"complete": True, "action": args.action}), flush=True)
        return 0
    except Exception as error:
        category = str(error) if isinstance(error, MigrationError) else type(error).__name__
        print(canonical({"complete": False, "category": category}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())

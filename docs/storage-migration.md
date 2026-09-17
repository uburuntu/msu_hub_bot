# Recovery artifact verification and import

`tools/migrate_storage.py` verifies preserved exports and imports their five entity types into the bot's PostgreSQL schema. It is an operator tool, separate from application startup and deployment. Run it with `uv run python -m tools.migrate_storage`. It reads immutable artifacts and connects only to PostgreSQL; it cannot export from a retired database backend.

Keep configuration, exports, diagnostics and recovery copies in private directories outside the repository. Configuration files must have mode `0600`; the tool creates private artifacts and prints only counts and error categories. Never pass credentials as command arguments.

## Connection configuration

The target JSON contains:

- `connection`: explicit `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` and optional TLS environment values.
- `expected_database` and `expected_system_identifier`: the intended database name and PostgreSQL cluster identity from `pg_control_system()`.
- `expected_schema_version`: the applied SQL migration number, defaulting to `3`. This differs from the public RPC contract version, which remains `1`.
- Optional `psql_command`: normally `["psql"]`. A local container transport may use `["docker", "exec", "-i", "-e", "PGPASSWORD", "-e", "PGUSER", "-e", "PGDATABASE", "supabase-db", "psql"]`. Forward environment variable names, never their values. The container must receive every connection variable it needs.

The administrative identity needs access to cluster identity, application tables and private observation helpers. Never give these privileges or platform credentials to the bot. The target guard checks the cluster, database, SQL revision and configured bot principal before transfer.

## Snapshot and exact import

Preserved exports contain a manifest and one JSONL file per entity type. Verification
checks schema, row counts, identities and hashes before any target writes. Format
`1` represents a full snapshot; format `2` also records source totals and an
optional receipt cutoff. Durable entity tables remain complete. Keep artifacts
unchanged and retain their independent native backups.

```sh
uv run python -m tools.migrate_storage verify --directory /private/baseline
uv run python -m tools.migrate_storage import \
  --target-config /private/target.json --directory /private/baseline \
  --target-writers-stopped
uv run python -m tools.migrate_storage reconcile \
  --target-config /private/target.json --directory /private/baseline \
  --target-writers-stopped
```

Canonical JSON preserves arbitrary JSON numbers without binary-float conversion,
JSON null, UUIDs, signed identifiers and microsecond timestamps. Unknown source
fields or relationships require an explicit mapping; imports never silently discard them.

Filtered reconciliation proves exact row and field parity for the declared receipt selection, alongside complete durable tables; it does not claim to have transferred excluded history. Missing records and unexplained target receipts, including older receipts outside the selection, fail raw reconciliation. Enrichment of user/chat profiles, memberships and topics comes only from included receipts. Historical observations found only in excluded receipts remain in recovery material and are not claimed as reconstructed entities.

Imports use bounded transactional COPY batches and absolute upserts keyed by source UUID. They never delete existing target records. Required JSON columns retain JSON null rather than becoming SQL NULL. Chat settings derive from object-valued `metadata.settings`; the complete source metadata remains intact. Creation times seed first-seen timestamps; the source snapshot marks the profile observation time.

Local checkpoints acknowledge successful batches. Rerunning the same import resumes; an uncertain commit is safely replayed. Keep batch size and manifest unchanged when resuming. Import and normalization checkpoints bind the complete manifest, including selection scope. `--replay` reapplies acknowledged batches, but cannot replay an export after its normalization has been applied. Target application writers must remain stopped during import and reconciliation: the flag is an operator assertion, not a mechanism that stops them. Reconciliation uses stable ordered streams and reports missing/extra rows and per-field hashes without printing values.

Before importing into an existing database, freeze application writers and verify
that the chosen artifact and target are authoritative for that recovery operation.
Extra target rows require review and are never automatically pruned. Production
PostgreSQL backups remain authoritative for writes made after an export.

## Message normalization and retention

First verify exact raw parity against the chosen export. Rehearse normalization in an isolated target; it can create additional observed identities. Keep the immutable export and verified native backup: normalized storage does not retain every historical message-edit body. Choose one timezone-aware `--as-of` instant and use it throughout preparation and validation.

For a filtered export, its recorded receipt cutoff must be at or before `--as-of` minus 30 days; a narrower export cannot prove coverage of that retention window. Normalization and reconciliation record both the export selection and the retention instant. Receipts are selected by `created`, while normalized message bodies expire by `sent_at`, including older messages nested in recent receipts.

```sh
uv run python -m tools.migrate_storage prepare-normalization \
  --directory /private/final --as-of 2030-01-01T00:00:00Z
uv run python -m tools.migrate_storage normalize \
  --directory /private/final --target-config /private/target.json \
  --as-of 2030-01-01T00:00:00Z --target-writers-stopped
uv run python -m tools.migrate_storage reconcile \
  --directory /private/final --target-config /private/target.json \
  --normalized --as-of 2030-01-01T00:00:00Z --target-writers-stopped
uv run python -m tools.migrate_storage audit-messages \
  --directory /private/final --target-config /private/target.json \
  --as-of 2030-01-01T00:00:00Z
uv run python -m tools.migrate_storage retention-report \
  --directory /private/final --target-config /private/target.json \
  --as-of 2030-01-01T00:00:00Z
```

The date above is illustrative. Preparation records each rejection in a private artifact and blocks application if any record cannot be transformed. Eligible messages that the typed extractor cannot represent also block application. Identity extraction uses the shared observer; stored bodies come from original JSON values. Receipts and embedded messages keep references, while each normalized body expires independently from its original message date. Entity profiles, memberships and topics use the same merge rules as runtime traffic.

Normalization is resumable and transactional. Its audit compares expected message keys, winning edit versions, full normalized rows and SHA256 body hashes. `retention-report` only counts expired and retained rows. An administrator applies bounded `msu_hub_private.retain_messages` batches separately after recovery checks. After retention, repeat reference reconciliation with `--normalized --retained-only --as-of ...`; remaining expired target receipts are reported as extras, and identity/settings/directory/subscription data must remain intact. Normalization may discover additional identities. The `validated` result requires preserved source fields and independently accounts each extra user/chat against the verified observation artifact; unexplained extras still fail validation.

## Recovery boundary

Supported recovery stays on Supabase using a compatible image and verified PostgreSQL backups. Image rollback cannot reverse a database cutover; cross-backend recovery remains blocked until a separate reverse transfer is implemented and verified. Preserve the mutation journal, deletion tombstones and current settings for that reconciliation. A frozen PostgreSQL backup is authoritative for post-cutover data: latest-only normalized messages cannot reconstruct every historical edit body. Verify counts and values before starting exactly one writer. The transfer tool never writes to the source database or runs a reverse migration automatically.

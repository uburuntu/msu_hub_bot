# Administrative storage transfer

`tools/migrate_storage.py` transfers the five source entity types into the bot's PostgreSQL schema. It is an operator tool, separate from application startup and deployment. Run it from the repository with `uv run python -m tools.migrate_storage`. The export operation also runs as a standalone script with the pinned EdgeDB driver installed.

Keep configuration, exports, diagnostics and recovery copies in private directories outside the repository. Configuration files must have mode `0600`; the tool creates private artifacts and prints only counts and error categories. Never pass credentials as command arguments.

## Connection configuration

The source JSON contains `connection` and `expected_database`. `connection` accepts the driver's explicit `credentials_file`, or connection fields such as `host`, `port`, `database`, `user`, `password`, `tls_security` and `tls_ca`. An address or credentials file is mandatory; ambient project discovery is insufficient.

The target JSON contains:

- `connection`: explicit `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` and optional TLS environment values.
- `expected_database` and `expected_system_identifier`: the intended database name and PostgreSQL cluster identity from `pg_control_system()`.
- `expected_schema_version`: the applied SQL migration number, defaulting to `3`. This differs from the public RPC contract version, which remains `1`.
- Optional `psql_command`: normally `["psql"]`. A local container transport may use `["docker", "exec", "-i", "-e", "PGPASSWORD", "-e", "PGUSER", "-e", "PGDATABASE", "supabase-db", "psql"]`. Forward environment variable names, never their values. The container must receive every connection variable it needs.

The administrative identity needs access to cluster identity, application tables and private observation helpers. Never give these privileges or platform credentials to the bot. The target guard checks the cluster, database, SQL revision and configured bot principal before transfer.

## Snapshot and exact import

An export uses one serializable, read-only, deferrable transaction for schema introspection, counts and every UUID-keyset page. It selects a bounded page of UUIDs before fetching the full records and verifies that the returned identities match exactly in order. Automatic retries are disabled. The manifest appears only after the entire snapshot commits; an interrupted export must start in a new directory. Baseline exports can use a restored native backup to reduce load on production.

Full export is the default. An explicit `--updates-since` selects only update receipts whose `created` timestamp is strictly greater than the supplied timezone-aware instant. Users, chats, directory entries and VK subscriptions remain complete. New manifests use format `2`, record the exact predicate, and include each table's source total, selected `count` and excluded count from the same snapshot. Verification checks both the counts and every included receipt's timestamp. Original format `1` full exports remain readable.

```sh
uv run python -m tools.migrate_storage export \
  --source-config /private/source.json --directory /private/baseline \
  --bot-id 999 --batch-size 100
uv run python -m tools.migrate_storage verify --directory /private/baseline
uv run python -m tools.migrate_storage import \
  --target-config /private/target.json --directory /private/baseline \
  --target-writers-stopped
uv run python -m tools.migrate_storage reconcile \
  --target-config /private/target.json --directory /private/baseline \
  --target-writers-stopped
```

The bot ID above is illustrative. Source introspection determines export fields, including computed names. Unknown fields and relationships require an explicit mapping; import never silently discards them. Canonical JSON preserves arbitrary JSON numbers without binary-float conversion, as well as JSON null, strings, arrays, UUIDs, signed identifiers and microsecond timestamps.

Use a filtered export only with a complete native backup whose checksum and isolated restore have been verified, an encrypted recovery copy outside the source host, and the preserved original source. For example, a retention instant of `2030-01-01T00:00:00Z` has the cutoff `2029-12-02T00:00:00Z`:

```sh
uv run python -m tools.migrate_storage export \
  --source-config /private/source.json --directory /private/retained-window \
  --bot-id 999 --updates-since 2029-12-02T00:00:00Z
```

Filtered reconciliation proves exact row and field parity for the declared receipt selection, alongside complete durable tables; it does not claim to have transferred excluded history. Missing records and unexplained target receipts, including older receipts outside the selection, fail raw reconciliation. Enrichment of user/chat profiles, memberships and topics comes only from included receipts. Historical observations found only in excluded receipts remain in recovery material and are not claimed as reconstructed entities.

Imports use bounded transactional COPY batches and absolute upserts keyed by source UUID. They never delete existing target records. Required JSON columns retain JSON null rather than becoming SQL NULL. Chat settings derive from object-valued `metadata.settings`; the complete source metadata remains intact. Creation times seed first-seen timestamps; the source snapshot marks the profile observation time.

Local checkpoints acknowledge successful batches. Rerunning the same import resumes; an uncertain commit is safely replayed. Keep batch size and manifest unchanged when resuming. Import and normalization checkpoints bind the complete manifest, including selection scope. `--replay` reapplies acknowledged batches, but cannot replay an export after its normalization has been applied. Target application writers must remain stopped during import and reconciliation: the flag is an operator assertion, not a mechanism that stops them. Reconciliation uses stable ordered streams and reports missing/extra rows and per-field hashes without printing values.

For the final cutover, stop the source writer and make a **new snapshot**, preserving complete durable tables and an explicit receipt scope. Repeat import and reconciliation for every mutable table. Creation timestamps are not change cursors. Keep the receipt cutoff fixed across baseline and final imports: advancing it can leave older target rows that reconciliation will correctly report as extras. Extra target rows, including records removed from the source, require deliberate review; they are never automatically pruned. Keep the source frozen until cutover validation succeeds.

## Message normalization and retention

First obtain exact raw parity after the final writer freeze and full resynchronization. Rehearse normalization in an isolated target: it can create newly observed identities whose UUIDs would otherwise conflict with identities subsequently created on the still-active source. Keep the immutable export and verified native backup: normalized storage does not retain every historical message-edit body. Choose one timezone-aware `--as-of` instant and use it throughout preparation and validation.

For a filtered export, `--updates-since` must be at or before `--as-of` minus 30 days; a narrower export cannot prove coverage of that retention window. Normalization and reconciliation record both the export selection and the retention instant. Receipts are selected by `created`, while normalized message bodies expire by `sent_at`, including older messages nested in recent receipts.

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

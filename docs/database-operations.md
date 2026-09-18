# Database operations

Use this guide for changes to the bot's PostgreSQL schema and Supabase RPCs.
The [database contract](../dbschema/postgres/README.md) defines ownership,
authorization and retention; [storage transfer](storage-migration.md) covers
imports and reconciliation. Host names, administrative connection profiles,
installed maintenance scripts and recovery credentials belong in the private
operator guide. Application CD never applies SQL or upgrades Supabase.

## Adding a field

For a feature document, follow [payload evolution](feature-persistence.md#evolving-models):
ordinary fields do not require SQL. The procedure below applies to a new relational
column, such as an observed user attribute, and releases the database change first:

1. Trace the field through `src/msu_hub_bot/storage/models.py`, observations, repository
   methods, SQL input allowlists, returned JSON, tests and recovery mappings.
   Define who writes it, how it is validated, and whether omission preserves
   it or explicit null clears it. A user preference must not be overwritten by
   an unrelated sparse Telegram observation or guessed from language alone.
2. Add the next zero-padded `NNN_description.sql` in `dbschema/postgres/`.
   Applied migrations are immutable; corrections use another migration.
   Begin a transaction, reject an unexpected preceding ledger revision, and
   use bounded lock and statement timeouts. Follow the existing migrations'
   explicit ownership and authorization conventions.
3. Add a nullable column without changing existing callers, for example:

   ```sql
   ALTER TABLE msu_hub_private.users ADD COLUMN is_premium boolean;
   ```

   Extend only the necessary private helper and reviewed RPC. Preserve old
   argument names, optional-input behavior and response types. New objects
   must belong to `msu_hub_owner`; functions use a fixed empty `search_path` and
   qualified object names. Revoke default/public execution and grant only
   the intended API signatures. A replaced function keeps its existing owner
   and grants, so verify them as well.
4. Record the new revision in `msu_hub_private.schema_migrations`, issue
   `NOTIFY pgrst, 'reload schema';`, and commit together with the DDL. Verify
   the refreshed API after commit. SQL migration numbers and the health RPC's
   API contract version are independent: an additive column does not require
   an API version bump. A breaking RPC contract needs a coexistence strategy.
5. Apply the tested database migration administratively, validate the old
   application against it, then deploy the new Pydantic field/repository code.
   `StoredRecord` ignores additional response columns; request models and SQL
   input allowlists are strict. New requests must not reach the old schema.
6. If needed, backfill in bounded, restartable primary-key batches with private
   checkpoints and explicit progress/count checks. Avoid replacing live writes
   or holding one transaction over the whole table. Add stronger constraints
   only after validating existing data and every active writer. Keep the old
   application compatible throughout its rollback window.

## Migration coordination

Before each SQL revision, search for consumers of the schema version and the
changed columns/functions, including installed operator tools:

- The PostgreSQL suites retain a schema-5 fixture for historical contracts and
  clone an isolated database for the complete migration history. Update the
  fixture, expected ledger and affected assertions; new tables also need cleanup
  and authorization coverage. Do not put sample SQL or manual one-off scripts in
  the numbered migration directory.
- `tools/migrate_storage.py` restores historical exports only into an isolated
  schema-5 target. It rejects other configured or actual revisions and guards
  every write batch. Finish import, reconciliation and normalization before
  applying later migrations to that target. Current recovery uses PostgreSQL
  backups containing feature records, jobs and transaction receipts together;
  old exports cannot reconstruct those records.
- The installed retention supervisor deliberately rejects an unreviewed SQL
  revision. It also pins the retention function body, owner and privileges,
  rejects unreviewed triggers/rules/inbound foreign keys on retention targets,
  and checks durable-table counts. Its private readiness record has a revision
  guard too. Review and test these controls for the new schema, update their
  reviewed expectations, and coordinate installation with the SQL application.
  Revalidate recovery/readiness evidence rather than merely changing its flags.
  If scheduling must pause briefly, finish with a dry run, bounded apply and a
  successful scheduled run; an enabled timer alone does not prove cleanup works.
  Never remove these guards to make a migration pass.
- Review backup/restore verification and durable-table coverage when adding
  relations. Decide explicitly whether they belong in the mutation journal or
  in retention. Message bodies must not enter durable profiles or tombstones.
- Feature storage also has a separate bounded `msu_hub_private.retain_features`
  helper. Schedule and verify it explicitly when enabling feature consumers;
  `retain_messages` does not clean feature records, terminal jobs or operation
  receipts. Preserve pending dependencies and validate its own ownership/grants.

## Consolidating application documents

Preferences, directory entries and VK subscriptions use permanent typed documents
in application scope `global` (`owner_id = 0`). Their maintained models live in
`storage/application.py`; ordinary field changes follow the feature model upgrade
contract instead of adding table-specific SQL.

| Source storage | Feature / collection | Document key |
| --- | --- | --- |
| `chat_settings` | `settings/chats` | Chat ID |
| `directory` | `ecosystem/chats` | Chat ID |
| `vk_subscriptions` | `vk/subscriptions` | `owner_id:chat_id` |

Use the following procedure for the numbered consolidation migrations:

1. Verify the exact database/cluster, preceding ledger, source counts, known
   preference types, document size bounds and an empty destination. Preserve a
   consistent recovery copy and validate its isolated restore. Review consumers,
   maintenance guards and backup coverage before changing the schema.
2. Pause CD and maintenance, hold the deployment lock and stop the sole poller.
   Install the feature foundation first if needed. Keep every application writer
   stopped through both migration stages; the tool does not stop them for you.
3. Apply `006_application_documents.sql`. It rejects destination collisions and
   invalid preferences, preserves source values and timestamps, and verifies an
   exact copy. The three preference defaults fill only missing keys. Directory
   and VK UUIDs/creation times stay in their payloads; all documents have no expiry.
   Read the administrative `verify_application_documents()` count report and
   validate the copied records through their Pydantic models without modifying them.
4. Apply `007_retire_application_tables.sql`. It repeats exact parity checks,
   then removes only the three source tables, their ten dedicated RPCs and the
   staging helpers. Unexpected dependencies abort the transaction; no cascading
   drop is used. User/chat journaling and historical journal entries remain.
   The generic chat RPC gains an explicit `p_refresh` policy for stale snapshots.
5. Verify authenticated health reports `application_documents: 1`, retained data
   and access restrictions, then start a compatible image. Startup/preflight reject
   an incomplete consolidation. Validate settings, directory and VK operations,
   game recovery and scheduled work. Reinstall reviewed maintenance expectations,
   run bounded cleanup checks and verify a scheduled run before resuming CD.

Before revision 7, source tables remain authoritative. Resuming old writers makes
the copied documents stale; stop again and explicitly reconcile before retirement.
After revision 7, an old image requiring the removed RPCs is not a valid rollback.
Use a schema-compatible image, or stop writers and reconcile a verified isolated
restore while preserving the current recovery copy. Feature writes are not covered
by the user/chat mutation journal. Never restore the whole shared Supabase instance
to undo one application's migration without assessing other projects' data.

## Other common changes

| Change | Safe approach |
| --- | --- |
| New table | Put it in `msu_hub_private`, owned by `msu_hub_owner`, with deliberate keys, foreign-key deletion rules and indexes. Enable RLS without ordinary-user policies; revoke table/sequence access from `PUBLIC`, `anon` and `authenticated`. Define its creation, update, deletion and recovery lifecycle. |
| New RPC | Expose only a versioned function in `msu_hub_api`. Authorize through `msu_hub_private.require_principal()`, enforce the intended data scope, validate inputs and use fixed search paths. Derive bot scope on the server for bot-scoped records. Use the existing restricted definer-owner pattern and grant execution only to `authenticated`. Test both allowed and denied calls. |
| Index | Check representative query plans first. For a busy table, consider `CREATE INDEX CONCURRENTLY`; it cannot run inside a transaction block. Use an explicitly reviewed nontransactional migration with preconditions, index-validity checks and documented recovery for an interrupted build. Record completion only after verification. |
| CHECK / foreign key | Where supported, add `NOT VALID`, correct existing data in batches, then `VALIDATE CONSTRAINT`. These constraints still apply to new writes. Adding an inbound reference to messages/updates changes retention safety and requires its own review. |
| Column rename / drop | Add the replacement first, keep compatible reads/writes, backfill and verify, then remove old consumers. Drop only in a later migration after the rollback window and a dependency check. Never use broad `CASCADE` as cleanup. |
| Type / NOT NULL | Assess conversion failures, table rewrites and lock duration on representative data. Prefer a replacement column with compatible writes and bounded backfill for large rewrites. Validate before enforcing constraints; do not hide failed values with lossy casts or invented defaults. |
| Data repair | Begin with read-only, bounded inspection and an explicit set of affected keys/counts. Prefer the existing RPC so validation and cache behavior remain consistent. Administrative SQL needs a guarded transaction or resumable batches, journal/recovery review and verification through the application; never run an unqualified update/delete. |

Definer functions execute as the table owner and bypass its own-table RLS;
their bodies must enforce authorization explicitly. Receipts/messages are
bot-scoped, while users, chats, settings, directory and VK subscriptions are
shared application records. These are not per-Telegram-user RLS policies.

For bot access revocation, disable its application principal first; subsequent
RPCs then fail even while a previously issued JWT remains unexpired. Retire
the Auth account/session separately. For planned credential rotation, use the
private operator procedure to update the dedicated Auth credential and runtime
secret source together, deploy, and verify login/refresh and principal identity.
Never rotate shared platform signing or service keys as if they belonged only
to this bot. Tokens and passwords must not enter shell arguments or public logs.

## Renaming application schemas

`ALTER SCHEMA ... RENAME TO` and an owner-role rename preserve existing object
identities and rows. They do not rewrite schema-qualified names embedded in
quoted SQL or PL/pgSQL function bodies. Review and replace those definitions
explicitly, including local row types and search paths, while preserving
signatures, owners and grants. Reject target-name collisions; never rename or
replace shared platform schemas as part of application cleanup.

Rehearse against the preceding SQL revision with representative data and a
verified isolated restore. Check unchanged application rows, table identities,
constraints and privileges, plus real Auth/RPC access and denied direct-table
access. Preserve the immutable migration history and original recovery copies.

Use a planned write pause: disable CD, hold the deployment lock, pause affected
maintenance and stop the sole poller. Keep the durable storage-transition guard
active while applying the guarded transactional rename and updating API exposure,
runtime profiles and maintenance expectations. Preserve other applications'
exposed schemas. Recreate services whose environment-based schema configuration
changed; `NOTIFY pgrst, 'reload schema'` alone cannot replace their environment.

Follow [deployment recovery](deployment.md#schema-rename-recovery) for failures
before or after SQL commit and compatible rollback configurations. Validate
bounded retention and its scheduled run, API readiness and a new verified backup
before resuming CD. Operator commands and protected configuration stay private;
SQL ledger revisions and RPC API versions remain independent.

## Validation and application

Run the Python checks appropriate to the code change and the real PostgreSQL
suite for every schema change. The SQL suite requires `psql` and a fresh,
disposable PostgreSQL instance with an empty database whose name starts with
`hub_test_`; it refuses existing application/Auth schemas. Migration tests also
exercise cluster-wide roles, so a second database in an already tested instance
is not a fresh fixture. An illustrative local connection is shown below:

```sh
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync ruff format --check src tests tools
uv run --no-sync mypy
uv run --no-sync pytest -q
HUB_TEST_POSTGRES_DSN=postgresql:///hub_test_schema_change \
  uv run --no-sync pytest -q tests/test_postgres_storage.py tests/test_feature_postgres.py tests/test_application_postgres.py
```

The ordinary suite skips real SQL without that variable; CI has a dedicated
PostgreSQL job. Test fresh installation and upgrade from the preceding schema
with representative synthetic existing data. Include omission/null behavior,
stale observations, replay/concurrency, grants and retention where affected.

Before production application, verify the exact database and cluster identity,
preceding ledger, migration checksum, active application versions and a recent
verified encrypted off-host backup with a tested restore path. Use the normal
administrator SSH connection and a private connection profile; never the bot
credentials, restricted deployment key or a credential-bearing command argument.
Coordinate with other platform users and overlapping database operations.
Apply only reviewed pending migrations with `psql -X -v ON_ERROR_STOP=1`; inspect
the outcome and ledger rather than assuming a client exit means every intended
step completed. Keep administrative output and identifiers private.

Afterward, verify ledger, ownership/grants, invariants and relevant query plans.
Exercise the real Auth → gateway → PostgREST path using protected credentials:
sign in, refresh, check the bot identity/API contract, and read/write a scoped
synthetic fixture through the changed RPC. Verify denial for an unrelated or
disabled principal and unavailable direct-table access; clean up only that
fixture. SQL tests use an Auth shim and cannot prove JWT or gateway behavior.
Check retention evidence, backup health, application readiness and privacy-safe
storage telemetry after deployment. Do not export tokens or real row bodies.

If a transactional migration fails, inspect the unchanged ledger and diagnose
before retrying. Prefer a reviewed forward fix after committed DDL; an image
rollback cannot undo database writes. A restore requires stopped writers,
reconciliation and a verified isolated restore first. Never overwrite the shared
platform to recover one application without assessing every other application's
data and preserving the current recovery copy.

Primary references: PostgreSQL [ALTER TABLE](https://www.postgresql.org/docs/17/sql-altertable.html),
[ALTER SCHEMA](https://www.postgresql.org/docs/17/sql-alterschema.html),
[function bodies](https://www.postgresql.org/docs/17/sql-createfunction.html)
and [CREATE INDEX](https://www.postgresql.org/docs/17/sql-createindex.html), and
PostgREST [schema cache reloads](https://docs.postgrest.org/en/v14/references/schema_cache.html).

# Database operations

Use this guide for changes to the bot's PostgreSQL schema and Supabase RPCs.
The [database contract](../dbschema/postgres/README.md) defines ownership,
authorization and retention; [storage transfer](storage-migration.md) covers
imports and reconciliation. Host names, administrative connection profiles,
installed maintenance scripts and recovery credentials belong in the private
operator guide. Application CD never applies SQL or upgrades Supabase.

## Adding a field

For example, an optional user-selected timezone needs a database change and an
application change, released in that order:

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
   ALTER TABLE hub_private.users ADD COLUMN timezone text;
   ```

   Extend only the necessary private helper and reviewed RPC. Preserve old
   argument names, optional-input behavior and response types. New objects
   must belong to `hub_owner`; functions use a fixed empty `search_path` and
   qualified object names. Revoke default/public execution and grant only
   the intended API signatures. A replaced function keeps its existing owner
   and grants, so verify them as well.
4. Record the new revision in `hub_private.schema_migrations`, issue
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

- `tests/test_postgres_storage.py` applies **every** top-level `*.sql` file in
  lexical order. Update its expected migration ledger and affected assertions;
  new tables also need fixture cleanup and authorization coverage. Do not put
  sample SQL or manual one-off scripts in that migration directory.
- `tools/migrate_storage.py` checks a configured `expected_schema_version`
  against the exact target revision and also has a fallback revision. Review
  the tool, synthetic fixtures, private target configurations and preserved
  export compatibility together; changing a number does not prove compatibility.
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

## Other common changes

| Change | Safe approach |
| --- | --- |
| New table | Put it in `hub_private`, owned by `hub_owner`, with deliberate keys, foreign-key deletion rules and indexes. Enable RLS without ordinary-user policies; revoke table/sequence access from `PUBLIC`, `anon` and `authenticated`. Define its creation, update, deletion and recovery lifecycle. |
| New RPC | Expose only a versioned function in `hub_api`. Authorize through `hub_private.require_principal()`, enforce the intended data scope, validate inputs and use fixed search paths. Derive bot scope on the server for bot-scoped records. Use the existing restricted definer-owner pattern and grant execution only to `authenticated`. Test both allowed and denied calls. |
| Index | Check representative query plans first. For a busy table, consider `CREATE INDEX CONCURRENTLY`; it cannot run inside a transaction block. Use an explicitly reviewed nontransactional migration with preconditions, index-validity checks and documented recovery for an interrupted build. Record completion only after verification. |
| CHECK / foreign key | Where supported, add `NOT VALID`, correct existing data in batches, then `VALIDATE CONSTRAINT`. These constraints still apply to new writes. Adding an inbound reference to messages/updates changes retention safety and requires its own review. |
| Rename / drop | Add the replacement first, keep compatible reads/writes, backfill and verify, then remove old consumers. Drop only in a later migration after the rollback window and a dependency check. Never use broad `CASCADE` as cleanup. |
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

## Validation and application

Run the Python checks appropriate to the code change and the real PostgreSQL
suite for every schema change. The SQL suite requires `psql` and a newly created,
empty disposable database whose name starts with `hub_test_`; it refuses existing
application/Auth schemas. An illustrative local connection is shown below:

```sh
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync ruff format --check src tests tools
uv run --no-sync mypy
uv run --no-sync pytest -q
HUB_TEST_POSTGRES_DSN=postgresql:///hub_test_schema_change \
  uv run --no-sync pytest -q tests/test_postgres_storage.py
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

Primary references: PostgreSQL [ALTER TABLE](https://www.postgresql.org/docs/17/sql-altertable.html)
and [CREATE INDEX](https://www.postgresql.org/docs/17/sql-createindex.html), and
PostgREST [schema cache reloads](https://docs.postgrest.org/en/v14/references/schema_cache.html).

# Bot database contract

Apply numbered SQL migrations administratively, in order, to the selected Supabase database. The image deployment never applies migrations. `msu_hub_private.schema_migrations` records applied SQL revisions; the health RPC reports the independent API contract version.

Migrations 001 and 002 retain their original `hub_private`, `hub_api` and `hub_owner` definitions. Migration 003 renames those objects to `msu_hub_private`, `msu_hub_api` and `msu_hub_owner` in place and updates schema-qualified function bodies. Its SQL ledger revision is **3**; RPC signatures and API contract version **1** are unchanged. Fresh installations apply the complete history, without rewriting earlier migrations.

Use [database operations](../../docs/database-operations.md) for routine schema changes, backfills, validation and recovery.

Migration 004 adds bot-scoped reaction snapshots and a bounded, chat-scoped leaderboard RPC. It preserves the existing API contract and archive callers; reaction observations are optional in archive requests. [Reaction tracking](../../docs/reactions.md) defines scoring, attribution and coverage.

Migration 005 adds typed feature documents, durable jobs and transaction receipts. Migrations 006 and 007 copy preferences, directory entries and VK subscriptions into permanent application documents, verify exact parity, then remove the three source tables and their dedicated RPCs. Writers remain stopped through both stages; follow the [consolidation procedure](../../docs/database-operations.md#consolidating-application-documents). Startup requires the health capability `application_documents: 1`, which is published only by the completed retirement migration.

Of this application's schemas, only `msu_hub_api` belongs in the Data API's exposed schemas. Its RPCs require an enabled `msu_hub_private.principals` record matching `auth.uid()`. Provision the dedicated Auth identity and its bot ID administratively; the runtime cannot choose its scope, register principals, access underlying tables, or invoke retention. Private tables enable RLS with no ordinary-user policies. The `msu_hub_owner` role owns application objects and definer functions; it cannot log in, administer roles/databases, inherit roles, or bypass RLS on other owners' tables. Outside the application schemas it receives only access to `auth.uid()`. Functions use explicit grants and fixed search paths.

The runtime signs in with a dedicated Supabase Auth account and publishable API key, keeps access/refresh tokens in memory, and refreshes on demand before expiry. One lock serializes authentication; failed refreshes cause a later fresh sign-in after a cooldown. RPCs are not automatically retried after uncertain outcomes. Every RPC checks the enabled principal, so disabling it blocks subsequent calls even with an unexpired access token. This account represents the bot, not its Telegram users: command-level authorization remains necessary.

The definer functions execute as the owner of these tables and can therefore bypass their RLS. Their explicit principal checks and operation-specific scope are the authorization boundary; RLS and revoked grants block direct client table access. Receipt/message scope comes from the principal's bot ID. Users, chats, preferences, directory entries and subscriptions are shared within this application's schemas. Other applications require separate schemas, owners, RPCs and credentials.

Imported identities preserve UUIDs, creation timestamps, signed identifiers, nullable values and arbitrary JSON metadata. Import user/chat `first_seen_at` and `last_seen_at` explicitly from known source observations; their defaults are appropriate only for new entities. Source `metadata` remains intact. The permanent `settings/chats` collection is authoritative for preferences and initially derives from object-valued `metadata.settings`; guarded patches update only supplied keys. Middleware and persistence share the `ChatPreferences` model.

New archive calls atomically record the receipt and its observed users, chats, memberships, topics and messages. Duplicate new `(bot_id, update_id)` receipts do not repeat writes. Set `is_legacy=true` on imported receipts so historical duplicates remain distinct. Administrative normalization uses the private `observe_archive` helper to apply the same observation merge rules to existing receipts, with an explicit retention instant for reproducible imports. Membership records describe observations and confirmed status changes, not a complete current chat roster. Profiles contain entity attributes, never whole message objects.

Archival is queued after handling, with its receipt timestamp captured beforehand. `handled` describes routing outcome, not successful completion of every external or background action. Supervised jobs have bounded concurrency/backpressure and drain on orderly shutdown; their queue is in memory. Failed archival is observable but has no durable replay queue, so history is not a guaranteed complete transcript of incoming or outgoing chat activity.

## Table lifecycle

All tables below belong to `msu_hub_private`. No automatic expiry applies unless listed.

| Table | Creation and updates | End of life |
| --- | --- | --- |
| `users` | Imported identity or observed Telegram user. Seen timestamps expand; sufficiently recent observations merge known profile fields without clearing absent fields. | Administrative removal only; no runtime delete. |
| `chats` | Imported identity, observation, explicit ensure or initial settings lookup. Seen/profile rules mirror users; settings lookup preserves existing profiles. | Administrative removal only; memberships and topics restrict deletion. Feature references are logical and require explicit cleanup decisions. |
| `feature_records` | Typed, versioned documents and child records written by guarded transactions. Ownership is either principal-derived bot scope or explicit application scope. | Optional expiry; permanent records use SQL NULL. Reads hide expired records immediately. Physical cleanup protects unfinished job dependencies. |
| `feature_jobs` | Durable schedules with leased execution, generation fencing, optional serial ordering and explicit retry/hold outcomes. | Unfinished work is retained; terminal jobs expire after seven days. |
| `feature_operations` | Canonical request hashes and response envelopes deduplicate transactions; receipts contain no payload bodies. | Seven days after creation. Replay reconstructs payloads from the matching caller request. |
| `chat_users` | Observed user/chat relationship, joins/leaves or membership updates. Tracks seen bounds and explicitly observed status/permissions. | Leaving updates status, retaining the row; no automatic deletion. This is not a complete current roster. |
| `chat_topics` | Observed forum thread; service events supply title/profile and closed/reopened status. Sparse observations preserve known values. | Closing changes a flag; no deletion synchronization or expiry. |
| `updates` | Receipt after handling, with kind, routing outcome and event data. Message bodies become references; duplicate new bot/update IDs are ignored. | Expires 30 days after receipt `created`. |
| `messages` | Accessible message observations, including replies/callbacks. Stores the latest observed version per bot/chat/message/business connection; retains original sending time. | Expires 30 days after `sent_at`; already-expired bodies are skipped on ingestion. References may outlive their targets. |
| `reaction_actors` | Latest selection for one bot/chat/message/actor, including empty removal tombstones. Event timestamp and update ID reject stale selection changes; late start/clear evidence can correct scoring. | Expires 30 days after accepted `event_at`; queries also limit the score window. No cascading message/receipt references or durable journal entries. |
| `reaction_counts` | Latest absolute anonymous snapshot for one bot/chat/message. Kept separate from identified people; paid quantities are not ordinary points. | Expires 30 days after `event_at`; no cascading references or durable journal entries. |
| `mutation_journal` | User/chat triggers record identity keys for inserts/updates and complete deletion tombstones; no-op updates are skipped. Historical preferences, directory and VK entries remain intact. | No scheduled pruning. Retire entries only against a verified recovery checkpoint and consistent durable snapshot. |
| `principals` | Administrator maps a Supabase Auth user to an enabled bot identity. Checked on every RPC. | Administrator disables or removes access; disable before retiring the Auth account. |
| `schema_migrations` | Successful administrative migrations append version/application time. | Permanent migration ledger. |

Permanent application collections use owner `application`, scope `global`:

| Feature / collection | Key and lifecycle |
| --- | --- |
| `settings/chats` | Chat ID. Lazy initialization or migration preserves defaults and unknown fields; patches preserve concurrent unrelated edits. No expiry or runtime delete. |
| `ecosystem/chats` | Chat ID. Explicit create, sparse patch or delete; source UUID and creation time remain in the payload. Listings may reference unobserved chats. |
| `vk/subscriptions` | `owner_id:chat_id`. Upsert preserves omitted options; cursor advancement is monotonic, while an explicit cursor patch may reset it. Suspension retains the record. No expiry or runtime delete. |

The [feature guide](../../docs/feature-persistence.md) covers model upgrades,
guarded writes, game state, scores and scheduled work. Generic feature RPCs serve
these collections; no settings-, directory- or VK-specific SQL API remains.

## Retention and recovery

The [feature persistence guide](../../docs/feature-persistence.md) defines typed
models, concurrency, upgrades and job recovery. Its separate administrative
`retain_features` helper performs bounded record/job/receipt cleanup without
changing the message-retention function. Enabling consumers requires installing
the schema, scheduling this helper and reviewing backup/maintenance coverage.

Update receipts expire 30 days after `created`; normalized messages expire 30 days after `sent_at`; reaction snapshots expire 30 days after `event_at`. Each message body belongs only to its normalized row: receipts contain message references, and normalized parent messages contain references to nested messages. Fresh replies, callbacks and edits cannot extend an older message body's lifetime. Non-message event payloads remain in their receipts. The administrative `msu_hub_private.retain_messages(batch, now)` function deletes at most the requested batch from each expiring table; repeat bounded batches until every deletion count is zero. It neither schedules itself nor deletes users, chats, feature documents or Redis state. Recovery backups have independent retention policies.

`mutation_journal` retains current user/chat changes and historical entries; it is not a change feed for feature documents. Recover records, jobs and transaction receipts together from a consistent PostgreSQL backup. Reconcile journal sequence checkpoints against the matching durable snapshot before pruning anything. Message bodies are excluded from the journal; recover them from retained message rows or an appropriate backup. An image rollback cannot reverse a data migration or restore retired RPCs.

Real PostgreSQL contract tests run when `HUB_TEST_POSTGRES_DSN` points to an empty disposable database whose name starts with `hub_test_`. They refuse existing application/Auth schemas, preserve historical contracts in a schema-5 fixture and clone an isolated database to test the complete migration history. They cover authorization, exact consolidation, rollback on failure, concurrent updates, replay, sparse observations, retained values and bounded retention. Use a fresh cluster for each run; the fixture creates and removes its own additional database. These tests supplement authenticated Supabase API checks; the Auth shim does not validate real JWT issuance or gateway configuration.

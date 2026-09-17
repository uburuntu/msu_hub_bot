# Bot database contract

Apply numbered SQL migrations administratively, in order, to the selected Supabase database. The image deployment never applies migrations. `hub_private.schema_migrations` records applied SQL revisions; the health RPC reports the independent API contract version.

Use [database operations](../../docs/database-operations.md) for routine schema changes, backfills, validation and recovery.

Only `hub_api` belongs in the Data API's exposed schemas. Its RPCs require an enabled `hub_private.principals` record matching `auth.uid()`. Provision the dedicated Auth identity and its bot ID administratively; the runtime cannot choose its scope, register principals, access underlying tables, or invoke retention. Private tables enable RLS with no ordinary-user policies. The `hub_owner` role owns application objects and definer functions; it cannot log in, administer roles/databases, inherit roles, or bypass RLS on other owners' tables. Outside the application schemas it receives only access to `auth.uid()`. Functions use explicit grants and fixed search paths.

The runtime signs in with a dedicated Supabase Auth account and publishable API key, keeps access/refresh tokens in memory, and refreshes on demand before expiry. One lock serializes authentication; failed refreshes cause a later fresh sign-in after a cooldown. RPCs are not automatically retried after uncertain outcomes. Every RPC checks the enabled principal, so disabling it blocks subsequent calls even with an unexpired access token. This account represents the bot, not its Telegram users: command-level authorization remains necessary.

The definer functions execute as the owner of these tables and can therefore bypass their RLS. Their explicit principal checks and operation-specific scope are the authorization boundary; RLS and revoked grants block direct client table access. Receipt/message scope comes from the principal's bot ID. Users, chats, preferences, directory entries and subscriptions are shared within this application's schemas. Other applications require separate schemas, owners, RPCs and credentials.

The five imported entity types preserve UUIDs, creation timestamps, signed identifiers, nullable values and arbitrary JSON metadata. Import user/chat `first_seen_at` and `last_seen_at` explicitly from known source observations; their defaults are appropriate only for new entities. The source `metadata` remains intact. `chat_settings.settings` is authoritative for preferences and initially derives from object-valued `metadata.settings`; settings patches update only supplied keys.

New archive calls atomically record the receipt and its observed users, chats, memberships, topics and messages. Duplicate new `(bot_id, update_id)` receipts do not repeat writes. Set `is_legacy=true` on imported receipts so historical duplicates remain distinct. Administrative normalization uses the private `observe_archive` helper to apply the same observation merge rules to existing receipts, with an explicit retention instant for reproducible imports. Membership records describe observations and confirmed status changes, not a complete current chat roster. Profiles contain entity attributes, never whole message objects.

Archival is queued after handling, with its receipt timestamp captured beforehand. `handled` describes routing outcome, not successful completion of every external or background action. Supervised jobs have bounded concurrency/backpressure and drain on orderly shutdown; their queue is in memory. Failed archival is observable but has no durable replay queue, so history is not a guaranteed complete transcript of incoming or outgoing chat activity.

## Table lifecycle

All tables below belong to `hub_private`. No automatic expiry applies unless listed.

| Table | Creation and updates | End of life |
| --- | --- | --- |
| `users` | Imported identity or observed Telegram user. Seen timestamps expand; sufficiently recent observations merge known profile fields without clearing absent fields. | Administrative removal only; no runtime delete. |
| `chats` | Imported identity, observation, explicit ensure or initial settings lookup. Seen/profile rules mirror users; settings lookup preserves existing profiles. | Administrative removal only; related settings, memberships and topics restrict deletion. |
| `chat_settings` | Imported preferences or lazy initialization from object-valued legacy metadata. Patches merge supplied keys and update the timestamp; legacy metadata stays intact. | No expiry or runtime delete. |
| `chat_users` | Observed user/chat relationship, joins/leaves or membership updates. Tracks seen bounds and explicitly observed status/permissions. | Leaving updates status, retaining the row; no automatic deletion. This is not a complete current roster. |
| `chat_topics` | Observed forum thread; service events supply title/profile and closed/reopened status. Sparse observations preserve known values. | Closing changes a flag; no deletion synchronization or expiry. |
| `updates` | Receipt after handling, with kind, routing outcome and event data. Message bodies become references; duplicate new bot/update IDs are ignored. | Expires 30 days after receipt `created`. |
| `messages` | Accessible message observations, including replies/callbacks. Stores the latest observed version per bot/chat/message/business connection; retains original sending time. | Expires 30 days after `sent_at`; already-expired bodies are skipped on ingestion. References may outlive their targets. |
| `directory` | Imported or explicitly created listing; commands/events patch its fields. Can reference chats the bot has not observed. | Explicit delete RPC; deletion is journaled. No expiry. |
| `vk_subscriptions` | Imported or explicitly upserted owner/chat pair. Publication advances its cursor; commands change options or suspension. | Suspension retains the row. No expiry or delete RPC. |
| `mutation_journal` | Triggers on users, chats, settings, directory and VK subscriptions. Inserts/updates record identity keys; deletes preserve the removed row; no-op updates are skipped. | No scheduled pruning. Retire entries only against a verified recovery checkpoint and consistent durable snapshot. |
| `principals` | Administrator maps a Supabase Auth user to an enabled bot identity. Checked on every RPC. | Administrator disables or removes access; disable before retiring the Auth account. |
| `schema_migrations` | Successful administrative migrations append version/application time. | Permanent migration ledger. |

## Retention and recovery

Update receipts expire 30 days after `created`; normalized messages expire 30 days after `sent_at`. Each message body belongs only to its normalized row: receipts contain message references, and normalized parent messages contain references to nested messages. Fresh replies, callbacks and edits cannot extend an older message body's lifetime. Non-message event payloads remain in their receipts. The administrative `hub_private.retain_messages(batch, now)` function deletes at most the requested batch from each message-bearing table; repeat bounded batches until both counts are zero. It neither schedules itself nor deletes users, chats, settings, directory entries, subscriptions or Redis state. Backups and the preserved source database have separate recovery policies.

`mutation_journal` records durable entity/settings/directory/subscription changes and deletion tombstones for reverse synchronization. Insert/update entries contain identity keys; delete entries preserve the removed row. Reconcile the monotonic sequence with a consistent current durable snapshot, and retire journal entries only after a verified recovery checkpoint. Message bodies are excluded; recover them from retained message rows or an appropriate recovery backup. A backend rollback must stop writers and reconcile changes before restarting the previous backend; selecting an old image alone is insufficient.

Real PostgreSQL contract tests run when `HUB_TEST_POSTGRES_DSN` points to an empty disposable database whose name starts with `hub_test_`. They refuse existing application/Auth schemas, install synthetic Auth identities, and exercise authorization, transactions, concurrent settings patches, replay, sparse observations, retained legacy values and bounded retention. Use a fresh database for each test run. The tests supplement authenticated Supabase API checks; the Auth shim does not validate real JWT issuance or gateway configuration.

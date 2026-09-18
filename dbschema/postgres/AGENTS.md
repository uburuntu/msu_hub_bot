# PostgreSQL persistence

- Keep application tables in `msu_hub_private`; expose only reviewed `msu_hub_api` RPCs. Every RPC must authorize the dedicated principal, use a fixed search path and have explicit grants.
- Preserve source UUIDs, timestamps, signed IDs and arbitrary metadata. Permanent application feature documents own preferences, the directory and VK subscriptions; legacy metadata remains intact.
- Raw receipts expire after 30 days from receipt; normalized messages expire from their original date. Retention never deletes durable entities or journals message bodies.
- Migration, import, retention and reverse synchronization are administrative operations outside normal image deployment. Validate SQL against a disposable database and the actual authenticated API.
- Follow [database operations](../../docs/database-operations.md) for fields, tables, RPCs, indexes, backfills and recovery. Append migrations; keep SQL ledger revisions separate from API versions and coordinate installed retention guards before advancing the schema.
- Schema renames must rewrite qualified names in quoted function bodies and coordinate API exposure, runtime profiles and namespace-aware rollback. Historical migrations retain the names they originally applied.
- Definer RPCs run as the table owner: authorize the principal and enforce scope inside each function. Bot credentials authorize application operations, not individual Telegram users; keep command authorization in handlers.
- For platform changes, inspect the deployed Supabase release's Compose files and service source. Keep an ignored `references/supabase` checkout; if missing, use `gh repo clone supabase/supabase references/supabase -- --depth=1`. Treat shared platform services, credentials and recovery separately from this application's schemas.

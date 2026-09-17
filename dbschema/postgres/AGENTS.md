# PostgreSQL persistence

- Keep application tables in `hub_private`; expose only reviewed `hub_api` RPCs. Every RPC must authorize the dedicated principal, use a fixed search path and have explicit grants.
- Preserve source UUIDs, timestamps, signed IDs and arbitrary metadata. `chat_settings` owns current preferences; legacy metadata remains intact.
- Raw receipts expire after 30 days from receipt; normalized messages expire from their original date. Retention never deletes durable entities or journals message bodies.
- Migration, import, retention and reverse synchronization are administrative operations outside normal image deployment. Validate SQL against a disposable database and the actual authenticated API.

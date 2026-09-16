# Legacy schema reference

- This directory describes EdgeDB types and historical migrations; deployment does not apply it automatically.
- Preserve schema/history during unrelated framework or command changes. Data migration is a separate operation with reconciliation and recovery requirements.
- Use the actual schema, not just Python model fields, when planning export: preserve metadata, relationships, uniqueness and timestamps.
- Keep each database's migration history explicit; do not relabel EdgeQL history as PostgreSQL migrations or infer export fields only from application models.

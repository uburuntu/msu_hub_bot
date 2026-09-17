# Persistence boundary

- [base.py](base.py) defines the repository contract; [models.py](models.py) owns typed records, sparse observations and patches. Backend adapters contain queries and transport details.
- Preserve the explicit EdgeDB adapter for recovery compatibility; legacy query helpers remain in [edb.py](edb.py). Do not translate arbitrary query strings between backends.
- Supabase calls use the authenticated, principal-gated RPC surface in [dbschema/postgres](../../dbschema/postgres/); keep privileged credentials and administrative migration operations outside runtime adapters.
- Preserve chat/user identity, metadata settings, timestamps and subscription cursors when changing persistence.
- Check actual query result shapes and missing-record behavior; type annotations alone do not establish those contracts.
- Review [schema definitions](../../dbschema/) alongside model changes and verify compatibility with existing records.
- Treat cache invalidation and first-contact record creation as part of the persistence contract.
- Preserve field presence separately from null, and keep message bodies out of durable entity profiles. Settings patches change only owned keys.

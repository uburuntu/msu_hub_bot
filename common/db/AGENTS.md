# Persistence boundary

- [edb.py](edb.py) contains the EdgeDB client, EdgeQL builders, Pydantic models and Telegram record types.
- Bot-specific directory and VK subscription models live in [hub_bot/db.py](../../hub_bot/db.py); callers access these helpers directly.
- Keep storage operations contained here rather than spreading database-specific queries through handlers.
- Preserve chat/user identity, metadata settings, timestamps and subscription cursors when changing persistence.
- Check actual query result shapes and missing-record behavior; type annotations alone do not establish those contracts.
- Review [schema definitions](../../dbschema/) alongside model changes and verify compatibility with existing records.
- Treat cache invalidation and first-contact record creation as part of the persistence contract.

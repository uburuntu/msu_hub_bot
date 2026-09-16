# Tests

- Tests use synthetic Telegram/database/provider data; network is disabled by default. Do not import live application state casually.
- Preserve behavioral contracts for retained aliases, filters, callbacks, FSM steps, permissions, text escaping and error cleanup.
- `fixtures/handler_inventory.json` is a routing contract: update it only for an explicit reviewed change, never to hide missing handlers.
- Audit reproductions may pass while proving a defect; distinguish those from assertions of intended behavior.
- Prefer focused regression tests for real risks. Migration gates also need clean Linux image/native checks and macOS developer checks; mocks alone do not establish compatibility.

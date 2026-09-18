# Feature persistence

- Keep the public API in `__init__.py`; models, transactions and leased work have separate modules.
- Stored payloads preserve unknown fields. Upgrade versions with pure functions; never overwrite future or invalid data with defaults.
- Mutations need exact revision/absence guards. Retry an uncertain commit with its frozen operation ID and request, not a rebuilt transaction.
- Pending work needs readable records: use no expiry until terminal. Leases cannot make external sends exactly once; handlers must classify safe retries and uncertain outcomes.
- Follow [feature persistence](../../../../docs/feature-persistence.md) for contribution, retention, evolution and recovery contracts.

# Telegram plumbing

- This layer contains the bot wrapper, extraction/filter helpers, middleware, storage and callback utilities.
- Follow [the command API](../../../docs/commands.md) for typed `MetaCommand` declarations, `MetaInfo`, managed inputs and shared replies. Keep the legacy filter/extractors compatible; use `register_command` in the existing router slot.
- [FSM storage](fsm_storage.py) and [scheduled deletions](deletions.py) use versioned feature documents. Preserve all aiogram key dimensions and atomic partial updates; event isolation remains process-local. [callbacks.py](callbacks.py) keeps process-local LRU text and locks.
- [Settings middleware](middlewares/settings.py) persists per-chat preferences through the storage repository; keep Telegram and storage contracts coordinated.
- Preserve authorization and chat preferences across message, callback, edited-message and automatic routes, including stale callback handling.
- [Rich input](rich_input.py) exposes nested text/media to explicit reply tools; keep attribution and quoted links out of automatic command/URL dispatch.
- Check callback state ownership, expiry and concurrent updates when changing interactive flows.
- Keep reply formatting, cancellation and middleware ordering explicit when adapting Telegram APIs.
- [Membership ingestion](../../../docs/memberships.md) commits direct status evidence before polling acknowledgement; preserve its persistent inbox and independent archive receipt lifecycle.
- Treat command parsing, state eligibility, callback formats and delivery context as compatibility contracts. Verify aiogram behavior in `references/aiogram` and Telegram semantics in `references/telegram-bot-api` from the repository root before choosing adapters or defaults.

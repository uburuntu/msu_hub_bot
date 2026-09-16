# Telegram plumbing

- This layer contains the bot wrapper, extraction/filter helpers, middleware, storage and callback utilities.
- [MetaCommand/MetaInfo and Extractor](filters.py) determine aliases, arguments, reply selection and media fallback; those details are part of command behavior.
- [storage.py](storage.py) uses Redis for FSM data, configuration and delayed deletions; [callbacks.py](callbacks.py) keeps separate process-local LRU text and locks.
- [Settings middleware](middlewares/settings.py) persists per-chat preferences through EdgeDB, so Telegram and storage changes require coordinated contracts.
- Preserve authorization and chat preferences across message, callback, edited-message and automatic routes, including stale callback handling.
- Check callback state ownership, expiry and concurrent updates when changing interactive flows.
- Keep reply formatting, cancellation and middleware ordering explicit when adapting Telegram APIs.
- Treat command parsing, state eligibility, callback formats and delivery context as compatibility contracts. Verify aiogram behavior in `references/aiogram` and Telegram semantics in `references/telegram-bot-api` from the repository root before choosing adapters or defaults.

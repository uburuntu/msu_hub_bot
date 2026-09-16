# Shared infrastructure

- [applets.py](applets.py) composes service initialization, startup and shutdown; command behavior belongs in `hub_bot/`.
- `db/`, `tg/` and `externals/` own persistence helpers, Telegram plumbing and provider adapters respectively.
- `config/` re-exports [msu_hub_bot.settings](../msu_hub_bot/settings.py); keep configuration ownership there.
- [executor.py](executor.py) aliases `PPExecutor` to `TPExecutor`: awaiting a timeout does not terminate the worker thread.
- Changes here can affect unrelated commands; trace callers and preserve documented return values, cleanup and error behavior.
- Make service lifetimes and cleanup explicit when changing shared infrastructure.
- Keep bot-specific jokes, command copy and feature choices in `hub_bot/`; shared helpers should support them without imposing a uniform tone.

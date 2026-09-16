# Provider adapters

- These modules contain provider HTTP calls, response parsing, demo protocols and media download adapters; command replies live in [hub_bot/commands/](../../hub_bot/commands/).
- An imported module or reachable home page does not prove its active command works; check the actual request and response path.
- [exceptions.py](exceptions.py) defines `ExternalServiceError` and user-readable failures; preserve caller contracts when normalizing transport and parsing errors.
- Bound the whole operation, including polling and retries, and own session/file cleanup explicitly.
- Prefer results and structured failures over Telegram objects; keep natural Russian/English explanations in the command layer.
- Provider retirement must account for every caller, callback, startup applet and configuration field; several adapters serve multiple commands.
- Remove only the intended adapter from mixed modules, and preserve the purpose of retained features when a backend changes.
- `_api2ch/` is isolated third-party source using `pydantic.v1`; access it through `dvach.py` and keep SDK changes separate from application model migrations.

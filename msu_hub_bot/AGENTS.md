# Runtime boundary

- Own typed settings, startup, preflight, health and redaction here; legacy feature code lives in `hub_bot/` and shared infrastructure in `common/`.
- `cli.py` currently bootstraps short legacy imports; changing this affects application import order and side effects.
- Validate optional provider configuration without making missing providers fatal to the whole bot. Keep administrator defaults restrictive.
- Preflight must not poll Telegram, send messages or run schema migrations; health should reflect actual poller progress.
- Coordinate settings migrations with framework/validation-library changes and saved-data compatibility. Never include secret values in exceptions or startup output.

# Runtime boundary

- Own typed settings, startup, preflight, health and redaction here; feature code lives in `hub_bot/` and shared infrastructure in `common/`.
- `cli.py` configures redacted logging and runs the composition root; use canonical imports without changing `sys.path`.
- Validate optional provider configuration without making missing providers fatal to the whole bot. Keep administrator defaults restrictive.
- Preflight must not poll Telegram, send messages or run schema migrations; health should reflect actual poller progress.
- Coordinate settings migrations with framework/validation-library changes and saved-data compatibility. Never include secret values in exceptions or startup output.

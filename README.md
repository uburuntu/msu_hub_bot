# MSU Hub Bot

A friends' Swiss-army Telegram bot, born in Moscow State University chats:
media tools, OCR, speech and song recognition, translation, code execution
through JDoodle, games, polls, chat administration, and a few inside jokes.

The runtime uses aiogram 2, EdgeDB, and Redis. Dependencies are locked for
reproducible builds. External tools depend on their providers' availability
and configuration; see the bot's `/help` for commands.

## Development

Use Python 3.11 and uv 0.12.15 or newer. Native development works on macOS; the
ACRCloud native SDK is installed only on Linux x86-64. FFmpeg and Tesseract are
needed for media and OCR commands.

```sh
uv sync --locked
uv run ruff check .
uv run pytest -q
cp .env.example .env
```

Fill in `HUB_BOT_TOKEN`, `HUB_REDIS_HOST`, and `HUB_EDGEDB_DSN`, then run:

```sh
uv run --env-file .env msu-hub-bot
```

Only one poller may use a Telegram token at a time. Local tests block network
access and use synthetic data; they require no live bot or database.

Configuration is documented by `.env.example` and the typed settings in
`msu_hub_bot/settings.py`. Lists, tuples, and mappings use JSON. Optional services
stay registered when unconfigured and return an unavailable response when used.
Administrator IDs default to no access. The Redis namespace defaults to `hub`;
changing it disconnects the bot from its existing state.

Real environment files, credentials, logs, dumps, private keys, and core dumps
are excluded from Git and Docker builds. Never put tokens in build arguments or
paste an expanded production Compose configuration into logs or issues.

## CI and production

Pull requests run locked Python checks on Linux and macOS, Ruff, tests, secret
scanning, workflow validation, and a Linux amd64 container smoke test. Actions
references and tool versions are pinned. Production credentials are confined
to the deployment job on the `production` environment.

Main-branch changes run the same checks, build and test the release image,
and transfer it directly to the VPS over SSH. The host verifies the archive
and runs its immutable image ID. Images are kept on the runner and VPS.
The bot reuses the existing Redis and EdgeDB services on the external `msu_db`
network. Deployment does not run migrations or recreate shared infrastructure.

The repository variable `DEPLOY_ENABLED=true` enables automatic deployment
from main. Pull requests run checks without deploying.

See [deployment operations](docs/deployment.md) for host setup, secrets,
rollback, and moving to another VPS.

## Maintenance and licensing

The launcher preserves the application's existing import layout. Ruff
exceptions are limited to specific legacy files; runtime, deployment, and test
code uses Ruff formatting.

Demotivators use Liberation Serif; animated text uses Ubuntu Mono. The debate
dataset and font assets are bundled.

Application license: **GPL-3.0-only**. Fonts retain their own licenses; see
[third-party notices](THIRD_PARTY_NOTICES.md). ACRCloud binaries remain an
external dependency; deployment images are transferred privately and are not
published for redistribution.

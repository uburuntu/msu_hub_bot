# MSU Hub Bot

- A friends' Swiss-army Telegram bot, born in Moscow State University chats and loved for doing useful, funny and occasionally silly things. Preserve that breadth and personality.
- Write natural, thoughtful Russian/English: concise for small actions, detailed when useful, with relevant links. Keep selected inside jokes; errors should help, not scold or bury the answer in boilerplate.
- Read `pyproject.toml`, `uv.lock`, runtime settings and deployment files for the implemented stack; do not duplicate changing version lists in guidance.
- Keep feature decisions traceable. User choices override inventory recommendations; a reviewed decision does not itself mean implementation, validation or deployment is complete.
- Keep simple things simple: prefer a small in-memory check to timers, persistence or extra services when the behavior does not need them.
- Respect the requested scope: planning does not authorize implementation, data migration, deployment or publication.
- Trace aliases, callbacks, FSM steps, automatic handlers and shared callers before removing a feature. Unreviewed dependent commands require review; preserve explicitly retained novelty features.
- Use `uv` and Ruff. Current checks: `uv run --no-sync ruff check .` and `uv run --no-sync pytest -q`; tests use synthetic inputs and block network. Run checks appropriate to the authorized change.
- Use Context7 MCP for library/framework/SDK/API/CLI/cloud documentation: resolve the library ID, then query current docs. Plain code review, scripts and business-logic refactoring do not require documentation lookup.
- Use `tvly` for web search; check `tvly --help` and the relevant subcommand help before use. Prefer primary sources and keep private context out of search queries.
- If network requests fail because of inherited proxies, retry with `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy`; do not change machine-wide proxy settings.
- Minimize work-in-progress items and documents in the tracked repository. Isolate scratch plans, research, review records and progress logs in ignored workspace storage when needed; promote only lasting, maintained knowledge into public documentation.
- Make documentation and comments durable: describe behavior, contracts and rationale; omit session status, temporary state and short-lived work narratives. Keep detailed execution tracking separate and clean up superseded working material.
- Keep credentials, environment files, chat data, dumps, logs and raw review notes out of public text and artifacts. Never copy private notes verbatim into documentation.
- Follow [the observability contract](docs/observability.md) for telemetry: shared boundaries own diagnostics, and exported data uses an explicit privacy allowlist.
- Production uses private image transfer over SSH and one poller per Telegram token. Preserve deployment isolation and rollback; database migration is a separate operation.
- Follow nested `AGENTS.md` files for local ownership. Keep them short and avoid repeating global rules; implementation notes belong with the relevant maintained contract, not in a growing guidance checklist.

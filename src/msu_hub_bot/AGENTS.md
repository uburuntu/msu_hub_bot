# Application

- [app.py](app.py) owns service composition, polling and shutdown; [cli.py](cli.py) configures redacted logging. Imports must not create clients, start work or change `sys.path`.
- [routing.py](routing.py) registers ordered feature routers. Preserve first-match order and stable handler keys; [events.py](events.py) owns membership/directory events.
- `commands/` owns behavior and copy; `telegram/` owns middleware, parsing and delivery; `providers/`, `storage/`, `media/` and `execution/` own their service boundaries. Keep shared helpers independent of individual command jokes.
- Middleware owns cross-cutting preferences, history, previews, topic isolation and telemetry. Handler dependencies remain explicit; service lifetimes and cleanup belong to composition.
- [settings.py](settings.py) is the configuration authority. Missing optional providers stay nonfatal; administrator defaults stay restrictive. Preserve saved configuration/data compatibility.
- Preflight checks dependencies without polling, sending messages or applying migrations. Health reflects poller progress; startup output and exceptions never expose credentials.

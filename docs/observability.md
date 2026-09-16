# Observability design requirements

These requirements govern Logfire and OpenTelemetry implementations through the aiogram 3 application lifecycle. Instrument shared boundaries so commands inherit useful diagnostics without repeating logging code. Telemetry must preserve command behavior, protect chat privacy and remain optional for bot availability.

## Trace ownership

| Boundary | Responsibility |
| --- | --- |
| Update outer middleware | Count received, ignored and rejected event kinds; measure dispatch overhead. Do not serialize the event. Filters run before a handler is selected. |
| Event-specific inner middleware | Start one `bot.handler` span after filters select a handler. Use a stable, explicitly registered handler key; aliases share it. Record the outcome and duration. |
| Provider adapter | Add a `provider.request` child span with a known provider, operation, attempt number and safe result category. |
| Media boundary | Add a `media.operation` child span around a meaningful conversion or worker operation; distinguish queue delay from execution time. |
| Storage boundary | Add a `storage.operation` child span with a named application operation, backend and outcome. |
| Background job | Own a bounded `job.run` span and failure handling. Link to the initiating trace when useful; do not keep an update span open for a delayed job. |

Callbacks and FSM steps use explicit handler keys, not callback payloads or state data. Dispatch failures before handler selection use a small, sanitized `bot.dispatch` failure span. Do not create a trace for each `getUpdates` request, empty poll, readiness heartbeat or ignored chat message.

Async tasks and executor work must propagate only the intended trace context and clear it on completion. Cancellation must not attach a worker's later failure to another update. Trace identifiers are correlation aids, not application identifiers or authorization inputs.

## Exported data

Construct telemetry from an allowlist before calling the SDK. The following fields are the maximum permitted categories, not a requirement to attach every field to every event.

| Allowed | Constraints |
| --- | --- |
| Service, environment and release | Fixed service name, deployment environment enum and public release identifier. No hostnames, filesystem roots or private repository metadata. |
| Handler, update kind and operation | Names chosen from application registrations or fixed enums. No arguments or dynamically generated names. |
| Outcome and failure category | Fixed values such as success, rejected, unavailable, timeout, cancelled or unexpected; a vetted exception class or provider error category. |
| Provider and backend | Configured type/name enum, not account, endpoint or instance identifiers. |
| Measurements | Durations, counts, retry number and coarse size buckets. HTTP status codes are allowed; response bodies are not. |
| Technical correlation | Random trace/span identifiers and safe module/function names from this repository. |

Never export message text, captions, prompts, speech/transcripts, replies, names, usernames, chat/user/message/update IDs, Telegram file IDs, callback data, locations, file contents or user-provided filenames. Do not replace personal identifiers with hashes: stable pseudonyms are still outside this contract.

Never export credentials, environment snapshots, connection strings, headers, cookies, full URLs, HTTP bodies, SQL/EdgeQL text or query parameters. Telegram credentials can appear in URL paths; provider keys can appear in query strings. Allowing a field called `url` is therefore insufficient protection.

Disable automatic argument/local-variable capture and baggage enrichment. Do not instrument entire modules, prints, model validation or HTTP/database clients globally. Introduce an automatic integration only after its complete exported payload passes the same allowlist. SDK scrubbing and existing local redaction are additional defenses, not substitutes for this boundary.

## Logs and failures

Keep existing redacted local logging. Export only a dedicated structured logging path with constant message templates and allowlisted attributes; do not forward the legacy update logger or every root logger record. Attach the export handler once and preserve existing console/file thresholds. Exporter diagnostics stay local to prevent recursive export failures.

One boundary owns each failure report. Children may set an error outcome, while the handler/job boundary emits one sanitized failure event. Known provider outages, missing configuration, invalid input, cancellation and stale callbacks use their own outcomes; normal rejection is not an unexpected incident. Preserve friendly user replies and redacted error-chat notifications without turning either into additional exported copies.

Do not send raw exception objects, `repr`, messages or formatted tracebacks to the SDK. Automatic exception capture must be disabled or replaced before exceptions cross an instrumented boundary: scrubbing does not guarantee removal of exception fields. Use vetted categories and, when needed, sanitized module/function/line frames without source lines, locals or absolute paths. Preserve a useful code location without reconstructing the input that caused the failure.

## Metrics and volume

Use counters for handler outcomes, provider attempts/failures and job results; histograms for handler, provider, queue and execution duration; gauges for active workers, queue depth and age of the last successful poll. Metric attributes come from small enumerations. Never label metrics by trace, task, exception text or personal identifier.

Measure polling health through aggregate counters and outage/recovery transitions, not per-poll logs. Record metrics independently of trace sampling. Set an explicit trace budget; sample whole traces consistently. Head sampling can discard a trace before its later error is known, so do not promise complete error retention when it is enabled. Tail sampling adds buffering and requires a measured memory budget. A collector or service mesh is not required for this design.

## Configuration and lifecycle

The application must receive only a project-scoped write token through `LOGFIRE_TOKEN`. `LOGFIRE_API_KEY`, read tokens and CLI login credentials are management/inspection credentials; do not inject them into the bot container, image, ordinary test jobs or application settings. Select the intended project and region explicitly during deployment setup; do not discover or create projects during bot startup.

Configure telemetry once in the composition root, before handlers run, with explicit service/environment/release metadata and an export enable switch. Missing or invalid telemetry configuration must fall back to redacted local diagnostics without preventing the bot from starting. Unit tests must explicitly disable remote export even when developer credentials exist; offline tests must require no credential lookup.

Use background batching with bounded queues, finite exporter request deadlines and bounded retries. Never perform synchronous export on a handler's event-loop path. Dropping telemetry under pressure is preferable to blocking commands. Readiness depends on bot/storage health, not Logfire availability.

Shutdown must stop new updates, drain owned application tasks, then flush and close telemetry within the deployment shutdown budget. Run blocking flush work off the event loop and configure the export transport's own deadlines. An async timeout does not kill a blocking exporter thread, and a flush timeout argument alone is not proof of a bounded shutdown. Test the actual SDK/exporter combination. A forced stop may lose telemetry but must not prevent a release or restart.

## Verification and rollout

Changes to instrumentation require offline capture tests for trace ownership, concurrency, cancellation, expected/unexpected failures, deduplication and bounded failure handling. Seed synthetic secrets and personal content in exceptions, nested attributes, URLs, logging extras, task context and inputs; assert they appear nowhere in exported traces, logs, metrics, resources or source metadata.

Use a separate test token and synthetic inputs for an explicitly enabled staging export. Inspect the intended project and region with separate read credentials. Verify handler/provider/job correlation, one failure event, usable safe code locations, low-cardinality metrics and acceptable overhead. Never start a second poller with the production bot token.

Promote only after privacy, delivery, queue saturation, exporter outage and shutdown checks pass. Keep export independently switchable so rollback restores local-only diagnostics without changing command, storage or deployment behavior. Logfire is distinct from the Logflare analytics component in a self-hosted Supabase stack.

## References

- [aiogram middleware boundaries](https://docs.aiogram.dev/en/latest/dispatcher/middlewares.html)
- [Logfire configuration reference](https://logfire.pydantic.dev/docs/reference/configuration/)
- [Logfire scrubbing](https://logfire.pydantic.dev/docs/how-to-guides/scrubbing/)
- [Logfire sampling](https://logfire.pydantic.dev/docs/how-to-guides/sampling/)
- [Logfire standard-library logging integration](https://logfire.pydantic.dev/docs/integrations/logging/)
- [OpenTelemetry Python trace SDK](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.html)

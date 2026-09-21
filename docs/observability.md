# Observability

These requirements govern Logfire and OpenTelemetry implementations through the aiogram 3 application lifecycle. Instrument shared boundaries so commands inherit useful diagnostics without repeating logging code. Telemetry must preserve command behavior, protect chat privacy and remain optional for bot availability.

## Runtime adapter

[`msu_hub_bot.telemetry`](../src/msu_hub_bot/telemetry.py) uses isolated OpenTelemetry providers and sends trace, metric and structured log protobufs directly to Logfire's EU endpoint. It does not configure global providers or the Logfire SDK, discover credentials, detect host resources, instrument clients automatically, or forward ordinary logging records. Existing logs remain local. Manual spans disable automatic exception capture.

The composition root constructs `TelemetryConfig()` with export disabled by default. Only the CLI explicitly reads the environment through `TelemetryConfig.from_env(env)`. Enabling export requires `HUB_TELEMETRY_ENABLED=true` and a project write token in `LOGFIRE_TOKEN`; `LOGFIRE_API_KEY` is never consulted. `HUB_ENVIRONMENT` accepts `local`, `test`, `staging` or `production`. `HUB_RELEASE` accepts a public hexadecimal release identifier or semantic version; other values are omitted. The SDK's additional `OTEL_SDK_DISABLED=true` kill switch is respected.

Register handler keys and literal command keywords before starting telemetry. A `Telemetry.operation(...)` accepts fixed boundary, provider/backend enums and allowlisted operation names; unknown names become `unknown`. Prefilter settings reads and passive update archival use `trace=False`: successful work contributes metrics without creating traces for ignored messages; an owning dispatch or job failure can still emit a sanitized incident. The executor measures queue wait, input preparation, awaited execution and actual completion separately. A completion after timeout contributes aggregate metrics only.

Trace head sampling defaults to 10%, configurable through `HUB_TELEMETRY_SAMPLE_RATE`, with at most 60 sampled root traces per minute. Children share the root sampling decision. Successful handler/job logs follow trace sampling; failure logs have a separate bounded queue and can survive an unsampled trace. Metrics do not depend on trace sampling; explicit views allow only application instruments and drop SDK internal metrics. Active work, queue depth and polling health use fixed aggregate instruments. Export queues and batches are bounded; pressure can leave an incomplete trace or lose a log. HTTP requests have a two-second total deadline, no redirects, cookies, ambient proxies or retries. A failed batch is dropped and exporter outage/recovery diagnostics remain local.

The exporter is an owned asyncio task with asynchronous DNS, not a blocking exporter thread. Shutdown reserves part of a three-second budget for transport cleanup and drops unfinished batches. SDK shutdown only closes the local processors and metric reader; no SDK background exporter or exit hook is registered. Tests decode real trace and metric protobuf payloads using an explicit capture transport while networking is blocked. A successful offline capture does not establish delivery to a Logfire project; that remains a separate, explicitly enabled staging check.

Native X media preparation owns one `media.operation` named `x.media.prepare`. Its operation handle accepts `media_asset(MediaKind, MediaReason, attempts=..., downloaded_bytes=..., duration=...)` once per selected unique asset. Reasons describe a ready upload, a ready reduced image, or a fixed omission category such as oversize, unsupported format, HTTP error, rejected redirect, empty response, timeout, network, I/O, exhausted budget or cancellation. Attempts count actual HTTP requests including redirects; bytes include streamed data from unsuccessful attempts. Never pass a URL, file ID, source ID, path, response body or exception to this accounting interface. Calls outside the owning active preparation are ignored.

The preparation span summarizes asset counts, attempts, omission categories and a coarse aggregate size. Partial omissions set an unavailable outcome, or timeout/cancelled when applicable; an escaping exception retains its own classification. Accounting never changes delivery behavior. Successful preparation logs follow trace sampling; omissions produce one `media.preparation.omitted` record through the bounded log queue, including when the trace is unsampled. Empty preparations produce no media summary log or asset measurements.

## Trace ownership

| Boundary | Responsibility |
| --- | --- |
| Update outer middleware | Count received, ignored and rejected event kinds; measure dispatch overhead. Do not serialize the event. Filters run before a handler is selected. |
| Event-specific inner middleware | Start one `bot.handler` span after filters select a handler. Use a stable, explicitly registered handler key; aliases share it. Record the outcome and duration. |
| Telegram request policy | Observe outgoing Bot API methods, attempts and fixed failure categories. Include numeric destination IDs without request contents. Polling uses aggregate metrics. |
| Provider adapter | Add a `provider.request` child span with a known provider, operation, attempt number and safe result category. |
| Media boundary | Add a `media.operation` child span around a meaningful conversion or worker operation; distinguish queue delay from execution time. |
| Storage boundary | Add a `storage.operation` child span with a named application operation, backend and outcome. |
| Background job | Own a bounded `job.run` span and failure handling. Link to the initiating trace when useful; do not keep an update span open for a delayed job. |

Callbacks and FSM steps use explicit handler keys, not callback payloads or state data. Command context comes from matched `MetaInfo` or `CommandObject`: only a registered keyword and its slash/hashtag kind, never arguments or a bot username. On-demand intent execution binds its chosen registered keyword with kind `mention`. Dispatch failures before handler selection use a small, sanitized `bot.dispatch` failure span. Do not create a trace for each `getUpdates` request, empty poll, readiness heartbeat or ignored chat message.

Polling explicitly subscribes to the installed Telegram client's update kinds except `inline_query` and `chosen_inline_result`. Passive reaction and membership events remain included. Successful reaction archival contributes storage and dispatch metrics without a per-reaction trace. Reactions never enter conversation state; their channel actor, if present, is distinct from a human user.

Middleware binds numeric request identity and the selected handler/command to a scoped context. Provider, storage and Telegram boundaries inherit it without changing handler signatures. Supervised jobs receive a fresh task context containing only those allowlisted fields and a trace link; they start independent spans rather than extending an update indefinitely. Other context variables and OpenTelemetry baggage are discarded. Scope exit and cancellation restore the previous context, so concurrent updates cannot borrow each other's identity. A worker's late completion contributes aggregate metrics rather than attaching to another update.

## Exported data

Construct telemetry from an allowlist before calling the SDK. The following fields are the maximum permitted categories, not a requirement to attach every field to every event.

| Allowed | Constraints |
| --- | --- |
| Service, environment and release | Fixed service name, deployment environment enum and public release identifier. No hostnames, filesystem roots or private repository metadata. |
| Handler, command, update kind and operation | Names chosen from application registrations or fixed enums. Command kind is slash, hashtag or mention. No arguments or dynamically generated names. |
| Telegram identity | Numeric `telegram.user_id`, `telegram.actor_chat_id`, `telegram.chat_id`, `telegram.message_id`, `telegram.update_id`, `telegram.thread_id` and `telegram.reply_to_message_id` when available. A reaction's channel actor remains separate from a human user. Outgoing requests may add `telegram.target_chat_id` and `telegram.target_message_id`. Never metric labels. |
| Outcome and failure category | Fixed values such as success, rejected, unavailable, timeout, cancelled or unexpected; a vetted exception class or provider error category. |
| Provider and backend | Configured type/name enum, not account, endpoint or instance identifiers. |
| Measurements | Durations, counts, retry number and coarse size buckets. HTTP status codes and bounded Telegram retry delays are allowed; response bodies are not. |
| Technical correlation | Random trace/span identifiers and safe module/function names from this repository. |
| Link source | `link.source_url` reconstructed from a recognized public post identity; `link.url_scope` distinguishes a post from an origin-only fallback. This explicit exception may retain an account handle inside a source path. Never a metric dimension. |

Numeric Telegram IDs deliberately support investigations across requests and help locate affected users or chats. Treat the Logfire project as private operational data: restrict project membership and read credentials, and avoid sharing raw records or dashboards publicly. This correlation is an explicit privacy tradeoff; it does not authorize message-content collection.

Never export message text, captions, command arguments, prompts, speech/transcripts, replies, names, usernames, Telegram file IDs, callback data, reaction emoji or custom emoji IDs, locations, file contents or user-provided filenames. Hashing a prohibited value does not make it permitted.

Never export credentials, environment snapshots, connection strings, headers, cookies, resolved media URLs, HTTP bodies, SQL text or arbitrary query parameters. The sole URL exception is the link source contract below. Telegram credentials can appear in URL paths; provider keys can appear in query strings. Allowing a field called `url` is therefore insufficient protection.

### Command argument resolution

`jev.resolve_languages` records optional translation argument inference. Explicit
supported language codes bypass the model. Numeric `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens` and `model.cost_usd` are bounded and recorded once;
`bot.model.input_tokens`, `bot.model.output_tokens` and `bot.model.cost` count
usage regardless of trace sampling, labelled only by fixed provider/operation.
Successful calls add no extra log; failures emit `bot.model.failed`. Prompts,
translations, language guesses and recent chat text are never exported.

### On-demand intent routing

An explicit mention with a reply owns one `process_intent` handler span. `jev.classify`
is its provider operation; `bot.intent.classified` logs its fixed result or failure
even when the trace is unsampled. Allowed result fields are `intent.command` from the
five-command catalogue or `none`, finite `intent.confidence`, provider-reported
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and `intent.cost_usd`.
Missing usage remains unknown. `bot.intent.input_tokens`, `bot.intent.output_tokens`
and `bot.intent.cost` count reported usage independently of sampling, with only the
fixed provider label. They exclude upstream charges for responses with no valid usage.

Selected execution uses `intent.execute` under the original request identity and
chosen command context. It measures execution separately without counting a second
handler. No instruction, replied-to contents, filename, full API response or history
is exported. An uncertain classification is not proof that a command ran; inspect
the execution operation and its outcome.

### Link extraction

`LinkService` owns one `links.preview` operation per recognized or attempted source. It records the terminal `link.stage`, `link.reason`, provider and outcome in a `bot.link.completed` log even when its trace is unsampled. Disabled preferences, conversation/command policy and unsupported routes are ignored outcomes, distinct from extraction failures and delivery failures. Duplicate links and candidates beyond the existing two-link limit do not create additional attempts. Ordinary unrecognized URLs with automatic previews disabled are not attempts.

Source metadata is scoped to the active preview and its same-task children, including Telegram delivery and X media preparation. It is not copied into worker or background-job contexts. [`source_metadata`](../src/msu_hub_bot/providers/link_source.py) reconstructs admitted YouTube, Instagram, TikTok, X, VK and selected generic video post paths, retaining only validated identity/timestamp parameters. Tracking queries and fragments are removed. Unknown routes expose their HTTPS origin only; credential-bearing URLs, IP/local hosts and known media CDN URLs are omitted. Values fit the span string limit without truncating a post URL. Treat these source links as private operational data alongside Telegram IDs.

Synchronous extractors collect at most 128 fixed `LinkDiagnostic` values per invocation. Killable subprocesses report bounded reasons and HTTP status rather than stderr or exception text. The awaiting task records these after completion; late worker results cannot mutate an ended preview. `bot.links.steps` counts concrete extraction/download stages; failed stages emit `bot.link.step.failed` and retain the source metadata. A recovered download failure can coexist with a successful terminal preview, so use terminal attempts for delivery reliability and step failures for troubleshooting.

`bot.links.attempts` and `bot.links.steps` use only provider, stage, reason and outcome dimensions. Existing X asset preparation also contributes one step per selected asset while a source scope is active, preserving its separate media measurements. URLs, Telegram IDs, HTTP response bodies and signed download links never enter metric labels or exemplars. Both counters are independent of trace sampling; logs remain subject to bounded queues and export loss.

Disable automatic argument/local-variable capture and baggage enrichment. Do not instrument entire modules, prints, model validation or HTTP/database clients globally. Introduce an automatic integration only after its complete exported payload passes the same allowlist. SDK scrubbing and existing local redaction are additional defenses, not substitutes for this boundary.

## Logs and failures

Local log formatters and explicit diagnostic/error replies filter credential fields marked `repr=False` in `Settings`, the runtime `LOGFIRE_TOKEN` and recognized credential patterns. Ordinary IDs, names, endpoints, email addresses, paths and schemas remain visible locally. Standard streams and logging records are not rewritten globally; fatal runtime failures go through the configured logger. Add `repr=False` when introducing a credential setting. It does not sanitize `model_dump()` or environment snapshots: never log those wholesale.

The telemetry adapter emits dedicated structured records with fixed bodies: `bot.operation.completed`, `bot.operation.failed`, `telegram.request.failed`, `media.preparation.completed` and `media.preparation.omitted`. Records carry the same safe context as spans, their operation/outcome/duration and trace/span correlation. Do not forward the legacy update logger or root logger; arbitrary `extra` fields and formatted messages must never reach the exporter. Exporter diagnostics stay local to prevent recursive export failures.

One handler/job/dispatch boundary owns the incident report. Children may record outcomes; a failed outgoing Telegram request also records its method, destination and a fixed reason because handlers may intentionally catch it. Known provider outages, missing configuration, invalid input, cancellation and stale callbacks use their own outcomes; normal rejection is not an unexpected incident. Preserve friendly user replies and redacted error-chat notifications without turning either into additional exported copies.

Do not send raw exception objects, `repr`, messages, notes, causes or formatted tracebacks to the SDK. Automatic exception capture must remain disabled: scrubbing does not guarantee removal of exception fields. Failures use vetted `error.type` and fixed `error.reason` values such as `chat_not_found`, plus permitted HTTP status or retry delay. Unknown exception classes collapse to a generic type. One verified application frame may provide `code.file.path` relative to the package, `code.function.name` and `code.line.number`; external or fabricated source paths are excluded. Source lines, local variables and absolute paths remain private.

## Metrics and volume

Metrics export every 60 seconds by default. `HUB_TELEMETRY_METRICS_INTERVAL_SECONDS` accepts 10–300 seconds; invalid values disable remote telemetry under the configuration contract. Logs and traces wake the exporter immediately and do not wait for this interval. Shutdown requests a final metric collection within its existing deadline.

Counters and histograms remain cumulative, retaining handler outcomes, durations and provider usage across collections. An unchanged series is still exported, so collection frequency directly affects quota use. A longer interval reduces measurement volume without sampling operations or failures; it also reduces gauge resolution and can miss short queue peaks. Read polling age together with sample freshness, and adjust dashboard freshness thresholds when overriding the default cadence. Rate charts need multiple samples per time bucket.

Use counters for handler outcomes, provider attempts/failures and job results; histograms for handler, provider, queue and execution duration; gauges for active workers, queue depth and age of the last successful poll. Metric attributes come from small enumerations. Never label metrics by trace, task, destination, exception text or personal identifier; IDs belong only to bounded trace/log records.

Measure polling health through aggregate counters and outage/recovery transitions, not per-poll logs. Record metrics independently of trace sampling. Set an explicit trace budget; sample whole traces consistently. A retained failure log may refer to a trace that was not sampled. Bounded queues and failed exports still allow loss, so do not promise complete incident retention. A collector or service mesh is not required for this design.

Media preparation adds `bot.media.assets`, `bot.media.download.attempts`, `bot.media.download.duration` and `bot.media.download.size`. Their only dimensions are fixed provider, media kind, reason and outcome values; reduced-image recovery counts as success. Download sizes are rounded up to 64 KiB, 1 MiB, 4 MiB, 16 MiB, 64 MiB or 128 MiB, with zero retained and a saturating 1 GiB bucket, before recording the histogram. Thus its sum, minimum and maximum also contain bucketed measurements. Exact byte counts remain local. Per-asset attempts and duration saturate at 1,000 requests and one hour; no user/chat identifiers or trace exemplars enter these metrics.

## Dashboards

See [background work and infrastructure checks](operations.md) for queue metrics,
held-job reconciliation and maintenance alerts.

Versioned Logfire dashboard definitions live in [`pulse.json`](../tools/observability/dashboards/pulse.json), [`usage.json`](../tools/observability/dashboards/usage.json), [`failures.json`](../tools/observability/dashboards/failures.json) [`jobs.json`](../tools/observability/dashboards/jobs.json) and [`links.json`](../tools/observability/dashboards/links.json). They are API create payloads with `definition.metadata.project` set to `PROJECT_NAME`. Counters provide totals independently of trace sampling; alias usage and record-based latency views describe sampled activity. Failure records can survive unsampled traces but remain subject to export loss. Keep definitions free of credentials, project identifiers and captured query results; deployed dashboards remain private operational data.

Link totals separate process identities and cumulative counter epochs. `metric_increase` omits a cumulative series' first reading, so the totals add it only when the counter's `start_timestamp` lies inside the dashboard's selected time range. Older streams contribute only their observed increase; delta streams need no correction. Rate charts require successive samples and remain estimates, so isolated first events belong in the totals and detail panels.

Follow the current [dashboard documentation](https://logfire.pydantic.dev/docs/guides/web-ui/dashboards/) and the selected region's `/api/openapi.json`. Use a separate, project-scoped management token with dashboard read/write permissions outside the bot runtime. Substitute the project name in a private payload copy; the API URL uses its UUID: `/api/v1/projects/{project_uuid}/dashboards/`. List before creating and match by slug. Before updating, save the existing definition and version: GET-one returns `{dashboard: definition}`, while POST/PUT return a dashboard object. PUT the revised definition with the current version; reread conflicts instead of overwriting them. Verify the saved definition and rendered panels, then retain `PROJECT_NAME` in the committed copy.

## Configuration and lifecycle

The application must receive only a project-scoped write token through `LOGFIRE_TOKEN`. `LOGFIRE_API_KEY`, read tokens and CLI login credentials are management/inspection credentials; do not inject them into the bot container, image, ordinary test jobs or application settings. Select the intended project and region explicitly during deployment setup; do not discover or create projects during bot startup.

Configure telemetry once in the composition root, before handlers run, with explicit service/environment/release metadata and an export enable switch. Missing or invalid telemetry configuration must fall back to redacted local diagnostics without preventing the bot from starting. Unit tests must explicitly disable remote export even when developer credentials exist; offline tests must require no credential lookup.

Use background batching with bounded queues, finite exporter request deadlines and bounded retries. Never perform synchronous export on a handler's event-loop path. Dropping telemetry under pressure is preferable to blocking commands. Readiness depends on bot/storage health, not Logfire availability.

Shutdown must stop new updates, drain owned application tasks, then flush and close telemetry within the deployment shutdown budget. Run blocking flush work off the event loop and configure the export transport's own deadlines. An async timeout does not kill a blocking exporter thread, and a flush timeout argument alone is not proof of a bounded shutdown. Test the actual SDK/exporter combination. A forced stop may lose telemetry but must not prevent a release or restart.

## Verification and rollout

Changes to instrumentation require offline capture tests for trace/log ownership, concurrency, cancellation, expected/unexpected failures, deduplication and bounded failure handling. Decode real OTLP payloads: verify identity propagation to providers and detached jobs, absence from metric labels, and context cleanup between updates. Seed synthetic secrets and prohibited content in exceptions, nested attributes, URLs, logging extras, task context and inputs; assert they appear nowhere in exported traces, logs, metrics, resources or source metadata.

Use a separate test token and synthetic inputs for an explicitly enabled staging export. Inspect the intended project and region with separate read credentials. Verify handler/provider/job correlation, one failure event, usable safe code locations, low-cardinality metrics and acceptable overhead. Never start a second poller with the production bot token.

Promote only after privacy, delivery, queue saturation, exporter outage and shutdown checks pass. Keep export independently switchable so rollback restores local-only diagnostics without changing command, storage or deployment behavior. Logfire is distinct from the Logflare analytics component in a self-hosted Supabase stack.

## References

- [aiogram middleware boundaries](https://docs.aiogram.dev/en/latest/dispatcher/middlewares.html)
- [Logfire configuration reference](https://logfire.pydantic.dev/docs/reference/configuration/)
- [Logfire scrubbing](https://logfire.pydantic.dev/docs/how-to-guides/scrubbing/)
- [Logfire sampling](https://logfire.pydantic.dev/docs/how-to-guides/sampling/)
- [Logfire standard-library logging integration](https://logfire.pydantic.dev/docs/integrations/logging/)
- [OpenTelemetry Python trace SDK](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.html)

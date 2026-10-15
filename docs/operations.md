# Background work and infrastructure checks

The [Logfire dashboards](observability.md#dashboards) combine unsampled operation
counts with sampled traces. Feature jobs add registered `feature` and `job.kind`
dimensions, retry attempts in traces, and acknowledged completion/retry/hold/expiry
counters. No job keys, scopes, payloads, lease tokens or user IDs become metric labels.

The worker reads queue summaries every 30 seconds independently of delivery.
Pending includes leased jobs; overdue excludes active leases. Held jobs require
reconciliation. Oldest-due age measures waiting work, not execution time. Snapshot
age includes time since the last exported measurement, so a stopped bot or exporter
cannot leave a fresh-looking value. It distinguishes a healthy empty queue from a
failed monitor: zero-valued queue series are not exported, and all queue gauges stop
emitting after 90 seconds without a successful read. A monitoring outage never
blocks a job or changes its state.

## Investigating a held job

1. Open Background workshop for queue age, pending/held work and recent job
   failures. Use Trouble desk for wider dependency failures and Pulse for the
   current service release before changing data.
2. With administrative access, run `tools/operations/feature_jobs.py` using a
   private connection file, an explicit `--feature` and `--state held`.
   Output contains private identities and scheduling metadata, never payloads.
   Keep it outside shared chats, issue bodies and committed files.
3. Check the owning feature record through its service and confirm whether the
   external effect happened. A timeout, lost lease or missing Telegram response
   does not prove a message was never sent. A live lease must finish or expire.
4. Use the feature's own recovery operation with its current revision. Reminders
   expose explicit retry/cancel actions in the Mini App; retry warns about possible
   duplicates. Game recovery must preserve the chosen result and scores while
   repairing delivery. A feature without a recovery action needs a targeted repair
   through its transactional service, with a preserved snapshot and regression test.
5. Verify the new job generation, record state and actual outcome. Never reset
   all held jobs, edit leases, delete receipts, or replace uncertain records with
   defaults. Queue inspection deliberately offers no generic replay switch.

## Investigating link previews

[Link workshop](../tools/observability/dashboards/links.json) is the reproducible
dashboard for native previews. `bot.links.attempts` records one terminal outcome
per considered link, up to two candidates per message. Delivery percentage compares
successful, failed and declined previews; disabled/policy skips, unsupported generic
yt-dlp links and cancellations stay separate. It does not measure every URL shared
in a chat. `bot.links.steps` counts concrete extraction attempts separately: an
earlier failed download can recover without making the final preview a failure.

Start with provider totals and terminal failure reasons, then inspect the bounded
recent-failures table for the sanitized source URL, chat/user/message/topic IDs,
HTTP status and trace. `bot.link.completed` and `bot.link.step.failed` logs are
emitted independently of trace sampling, but bounded export can lose records;
check Pulse freshness before interpreting missing evidence. Source URLs and
identities are private diagnostic context, never metric labels or public reports.
Use the [observability contract](observability.md) for the export allowlist and
dashboard management. A failed step alone does not justify retrying a Telegram
send whose result is uncertain.

## Message diagnostics

`/json` shows the replied-to message (or the command itself) with complete Telegram
IDs and hidden credentials. Dumps that exceed Telegram's text limit arrive as a
JSON file, preserving the complete message and valid JSON. `/echo` uses the same
diagnostic output before copying the message.

The owner-only `/logs` command and local logs preserve Telegram IDs and ordinary
configuration values while hiding credentials. See the [credential filtering
boundary](observability.md#logs-and-failures) before adding settings or diagnostics.

## Scheduled operational alerts

`tools/operations/monitor.py` checks successful maintenance receipts and free space:

| Check | Threshold |
| --- | --- |
| Encrypted backup | Successful completion within 36 hours |
| Retention | Successful bounded run with durable entities unchanged within one hour |
| Isolated restore check | Verified restore within 30 days |
| Disk reserve | At least 40 GiB and 10% free |

Pass `--evidence`, `--restore-receipt` and `--disk` from private host configuration.
Run without `--notify` to inspect its sanitized JSON report. `--notify --state PATH`
uses the running bot's token to notify its configured owner; no secret is copied
into the tool or scheduler. A problem must persist for ten minutes before it is
reported, which absorbs receipts rewritten while maintenance is running. Alerts then
fire when the reported set changes and at most once every six hours for an unchanged
problem; recovery of a reported problem sends one message.
Transport failure does not acknowledge an alert. Schedule the check every five
minutes using the host's service manager, with a process timeout and private state.

The monitor never runs retention, creates/prunes backups, transfers data off-host,
restarts services or treats a backup as proof of restoration. Update the configured
restore receipt only after an actual isolated restore check. Missing or malformed
evidence is a failed check, not evidence of a successful empty run.

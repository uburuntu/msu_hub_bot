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
failed monitor; queue gauges stop
emitting after 90 seconds without a successful read. A monitoring outage never
blocks a job or changes its state.

## Investigating a held job

1. Open Pulse's feature queue panel, then Trouble desk for that feature/kind.
   Inspect failures and the current service release before changing data.
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
into the tool or scheduler. Alerts fire when the set of problems changes and at
most once every six hours for an unchanged problem; recovery sends one message.
Transport failure does not acknowledge an alert. Schedule the check every five
minutes using the host's service manager, with a process timeout and private state.

The monitor never runs retention, creates/prunes backups, transfers data off-host,
restarts services or treats a backup as proof of restoration. Update the configured
restore receipt only after an actual isolated restore check. Missing or malformed
evidence is a failed check, not evidence of a successful empty run.

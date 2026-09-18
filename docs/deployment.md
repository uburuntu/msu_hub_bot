# Deployment operations

## Host contract

The application uses `~/msu_hub_bot` on the deployment host. The SSH deployment
key is separate from personal SSH keys and restricted to the installed host
wrapper as a forced command. Its source is [tools/deployment/host.py](../tools/deployment/host.py).
The wrapper accepts only deploy/rollback requests and
uses a host lock. A deployment sends one JSON header followed by a compressed
image archive over the same encrypted SSH connection.

Requirements: Linux x86-64, Python 3, Docker, Compose 2.30 or later, and access to
Redis and the selected database backend through Docker network `msu_db`. The account
must be able to use Docker. Passwordless sudo is not required.

Install the reviewed wrapper as `~/msu_hub_bot/deploy.py` in a mode-700 directory.
Add a dedicated Ed25519 public key to the deployment account's authorized keys:

```text
restrict,command="/usr/bin/python3 /home/DEPLOY_USER/msu_hub_bot/deploy.py" ssh-ed25519 PUBLIC_KEY msu_hub_bot-actions
```

Use the actual home path when moving hosts. Wrapper changes require the normal
administrator SSH connection; the restricted CI key cannot upload executable
scripts or run arbitrary commands. Keep the deployment directory and SSH files
private to their owner.

### Updating the host wrapper

Actions transfers images and configuration; it does not update `deploy.py`.
For a wrapper change, use the administrator connection and wait for deployment
and rollback jobs to finish. Upload the reviewed file beside the installed
wrapper with owner-only permissions, check its SHA-256 against the reviewed
source, and check its syntax with the host's Python. Under an exclusive lock on
`~/msu_hub_bot/deployment.lock`, retain a private copy of the installed wrapper
and atomically replace it with the checked file. Verify the installed checksum
before releasing the lock. Preserve the existing restricted SSH command.

Installing a wrapper does not alter running containers or saved releases.
Resource changes take effect when the wrapper generates a new release's Compose
file; rollback continues to use the preceding release's saved configuration.
Restore the saved wrapper under the same lock if the wrapper itself needs recovery.

### Resource budget

The generated service configuration bounds both preflight and the bot:

| Resource | Ceiling |
| --- | --- |
| CPU | 3 CPU equivalents |
| Memory | 4 GiB, including temporary files; swap disabled |
| Processes and native threads | 128 |
| `/tmp` | 512 MiB |
| `/work` | 512 MiB |

These ceilings leave capacity for other applications on a shared host with six
CPUs and 16 GiB of RAM. Three concurrent media jobs must fit inside the bot's
budget; input, decoded-media and admission limits provide the earlier user-facing
rejections. Container limits are the final boundary, not a substitute for those
checks. Docker's init process forwards shutdown signals and reaps orphaned
children. Both temporary filesystems count toward the memory ceiling and disappear
on container removal. Media intermediates, runtime caches and local logs use
`/tmp`; the warning log retains a 10 MiB current file and two backups, preserving
`/logs` within a 30 MiB total. The working directory remains independently bounded.

Before changing ceilings, stage the reviewed image in a uniquely named disposable
container with networking disabled, no runtime credentials, and an explicit
Python entrypoint instead of the bot. Apply the generated service's limits and
read-only filesystem. Exercise three concurrent representative conversions,
near-limit images/video, OCR and animation, then bounded PID and temporary-storage
saturation with cleanup. Record durations, cgroup `memory.peak`, `memory.events`,
`pids.peak`, `pids.events` and CPU throttling privately. Normal conversions must
succeed without OOM or PID-limit events; intentional saturation must reject work
and recover. Repeat on the target architecture before rollout.

Validate the normalized Compose configuration, then inspect the created
container's `HostConfig.NanoCpus`, `Memory`, `MemorySwap`, `PidsLimit`, `Init` and
`Tmpfs`. After deployment, verify those fields again alongside health, restart
count and polling progress. See Docker's
[service configuration](https://docs.docker.com/reference/compose-file/services/)
and [tmpfs accounting](https://docs.docker.com/engine/storage/tmpfs/).

## GitHub configuration

Create a `production` environment permitting deployments from `main` only.
Required reviewers are intentionally not enabled.

| Setting | Storage |
| --- | --- |
| `HUB_*` application configuration from `.env.example`, except telemetry controls below | Production environment secrets |
| `LOGFIRE_TOKEN` (optional project write token) | Production environment secret; sent only when export is enabled |
| `HUB_TELEMETRY_ENABLED`, `HUB_TELEMETRY_SAMPLE_RATE` | Production environment variables; defaults `false` and `0.1` |
| `HUB_ENVIRONMENT`, `HUB_RELEASE` | Set by the workflow to `production` and the public release revision |
| `DEPLOY_SSH_KEY` | Production environment secret; dedicated private deployment key |
| `SSH_HOST`, `SSH_USER`, `SSH_PORT` | Production environment secrets |
| `SSH_KNOWN_HOSTS` | Production environment secret; independently verified host keys |
| `DEPLOY_ENABLED=true` | Repository variable; enable after bootstrap |

GitHub is the source of truth for runtime settings. For example, set an updated
value through stdin with `gh secret set HUB_BOT_TOKEN --env production --repo
uburuntu/msu_hub_bot`, then dispatch the Deploy workflow. Do not place secret
values in `--body` arguments or shell history. Arrays and mappings must be JSON.

Actions builds and tests the image before the transfer step receives any
production configuration. Images are transferred directly over SSH. The host
checks the archive checksum, image ID, revision label, platform, non-root user,
entrypoint, and tag before loading it. Archives containing extra image tags,
unsafe paths, or embedded runtime settings are rejected.

The workflow sends configuration over authenticated SSH. The wrapper stores a
mode-600 `runtime.env` for each release, outside any checkout. Its single JSON
envelope is read using Compose's raw environment-file mode, preserving quotes,
dollar signs, and multiline values without interpolation.

Optional telemetry uses the same private envelope and remains disabled unless
`HUB_TELEMETRY_ENABLED` explicitly enables it. Disabled deployments omit
`LOGFIRE_TOKEN`; a saved token alone cannot enable export. Before the first
enabled deployment, verify that the installed host wrapper accepts this exact
additional variable. Keep
management API keys, read tokens and CLI credentials out of runtime settings;
`LOGFIRE_API_KEY` and arbitrary `LOGFIRE_*`/`OTEL_*` variables are not accepted.
Build and image-validation steps receive no project token. See the
[observability contract](observability.md) before enabling export.

## Database configuration

`HUB_STORAGE_BACKEND` defaults to `supabase`, the only supported application backend.
Redis remains required for topic conversations and scheduled deletions. Keep
its namespace and database unchanged during application upgrades. Chess and
geoguess use Supabase for all persistent state, including daily scores; see
[quiz storage and cutover](feature-persistence.md#chess-and-geoguess).

Supabase requires
`HUB_SUPABASE_URL`, a publishable `HUB_SUPABASE_KEY`, and a dedicated Auth account
in `HUB_SUPABASE_EMAIL` / `HUB_SUPABASE_PASSWORD`. `HUB_SUPABASE_SCHEMA` defaults
to `msu_hub_api`. The server must authorize that principal for the bot; readiness
checks verify both the RPC API contract version and bot identity. The application
signs in and refreshes short-lived tokens over the API. It does not need a
PostgreSQL password, service-role key or platform signing secret.

Provision the full Supabase platform, application schema, principal grants,
retention and backups separately from application deployment. Install the
reviewed host wrapper before sending Supabase configuration. CI exercises SQL
contracts in an empty disposable PostgreSQL database; its credentials never
refer to production. Runtime readiness validates the actual API path.

## Cutover and rollback

Before stopping the current bot, the wrapper verifies and loads the image and runs a
separate preflight: configuration validation, Redis ping, the selected database's readiness check,
Telegram `getMe`, and required media programs. Preflight never polls Telegram,
sends messages, or migrates the database.

When an existing `hub_bot` container is present, the first cutover retains it,
disables its restart policy, and explicitly
signals its Python process because the old shell entrypoint does not forward
signals. It stops the old container before starting `msu_hub_bot`. Subsequent
deployments also stop the current poller before starting its replacement.

Successful polling updates a readiness heartbeat. A release has five minutes
to become healthy; shutdown has a 90-second allowance. Failure stops the new
poller before restoring the previous image and configuration when both releases
use the same database backend and, for Supabase, the same API schema. Initial rollback
restores that container and its original restart policy. A host without an
existing container can deploy directly; manual rollback becomes available
after a second successful release.

Use **Actions → Rollback → Run workflow** from main to restore the preceding
release. `current.json` and `previous.json` record revision, image ID, and release
directory plus the storage backend and Supabase API schema. Stored runtime configuration is sensitive; do not attach these
directories to issues or CI artifacts. Container logs are rotated locally.
Failed Docker operations and startup logs are retained privately in the
release's `failure.log`.

The current and preceding release archives are retained on the VPS. Rollback
can reload the preceding image if it was removed from Docker's local cache.
Older generated release directories and unused application images are cleaned
up after a successful deployment. Shared database containers and other
applications are outside this cleanup.

Application rollback requires the same Supabase API namespace and compatible
configuration. The wrapper recognizes retired backend identities in historical
release records to prevent resuming a different database writer. Those records
are not permission to restore a retired backend. Recovery uses verified
PostgreSQL backups and a compatible Supabase release; image rollback cannot
copy or reverse database writes.

Before starting a release that changes backend or Supabase API schema, the wrapper records a private
`storage-transition.json` recovery marker. It removes the marker only after the
healthy release's current and previous records are safely published. An interrupted
or failed transition blocks every later deploy and rollback request, including
requests for the old backend or namespace. An administrator must reconcile the data, establish
the authoritative release records, and archive the marker under the deployment
lock before resuming releases. The restricted Actions key cannot clear this guard.

### Schema rename recovery

The storage compatibility guard includes `HUB_SUPABASE_SCHEMA`. Older Supabase
release records obtain it from validated saved runtime configuration; a missing
setting in that historical configuration means the original `hub_api`. Missing
or malformed release files must not silently authorize rollback. A schema rename is
an administrative operation with a planned bot pause, outside application CD;
see [database operations](database-operations.md#renaming-application-schemas).

Before cutover, prepare and rehearse the proven image with a new immutable
release configuration selecting the renamed API. Preserve its original release
files. If the DDL transaction fails, verify rollback before restarting the
original configuration. Once DDL commits, recovery uses the proven image with
the new namespace; an automatic restart using the old namespace is unsafe.
Keep the transition guard active until an administrator verifies the resulting
contract and establishes authoritative compatible release records under the
host lock. Deploy the candidate with the proven, newly configured release as
its rollback target. Resume CD only when both current and previous releases use
the renamed namespace and polling, API access and maintenance checks pass.

## Conversation resets across FSM generations

Ordinary restarts preserve pending conversations. When upgrading from the
legacy FSM to the topic-aware FSM, or rolling back across that boundary, start
with clean conversations instead. Pause deployment and rollback jobs, stop
every poller using the configuration, and wait for shutdown before resetting.
Use the administrator connection; the restricted deployment key cannot run
maintenance commands.

Use a reviewed, locally available application image containing
`msu_hub_bot.fsm_reset`. The older rollback image may not contain this tool.
Supply the existing private release environment file without printing it:

```sh
docker run --rm --network msu_db \
  --env-file /private/release/runtime.env \
  --entrypoint python REVIEWED_IMAGE \
  -m msu_hub_bot.fsm_reset --generation legacy
```

Repeat with `--generation v3` before starting the target release. Each command
prints only the number of removed keys and exits nonzero on failure; a failed
reset may have removed some keys and can be repeated while polling stays
stopped. Then deploy or roll back normally and verify one healthy poller.

The command reads only `HUB_NAME` and `HUB_REDIS_*` from the deployment envelope
or environment. It opens only Redis: no Telegram requests, Supabase connection or
schema migration. `legacy` matches the namespace's chat/user FSM state/data;
`v3` matches its `fsm3` topic FSM state/data. Settings, delayed deletions and
other namespaces remain intact. Never use `FLUSHDB` for
this operation. Startup, Deploy and Rollback do not run the reset automatically.

## Moving VPSs

Provision Docker and the required database connectivity, install the reviewed
wrapper and a new restricted key, verify the new host key independently, and
update the production SSH secrets. Runtime application secrets
stay in GitHub. Stop the old host's bot before activating the new one.

Moving or restoring databases, changing TLS trust, and applying schema or
retention migrations are separate operations. Deployment never applies
`dbschema` migrations or resets conversation state automatically. Shared
database services and other applications are outside the release wrapper's authority.

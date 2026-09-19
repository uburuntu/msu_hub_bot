# Telegram Mini App

The Mini App provides reminders, chat preferences, repost configuration and
community statistics through a reusable React shell. `web/src/platform/` owns Telegram and HTTP integration, `app/` owns
navigation, and `features/` owns each tool. Python's `web/` package supplies
the authenticated adapter; `reminders/` supplies behavior shared with commands.
Future tools compose into these boundaries rather than adding database calls
to UI components. See [frontend development](../web/README.md).

## Identity and destinations

Every API request carries Telegram's raw init data in the `Authorization`
header. The server checks its HMAC with the bot token, rejects duplicate fields,
validates a human user, and limits age to one hour with 30 seconds of clock
skew. Client-provided user IDs and `initDataUnsafe` never authorize anything.
No credentials or drafts are saved in browser storage. Reopening through
Telegram refreshes authentication; the UI preserves a draft during ordinary
network failures and list refreshes.

The private bot menu defaults creation to the user's private chat. `/app` and
reminder buttons sign the requesting user's chat/topic into a two-hour launch
token. Group buttons first open a private `/start` bridge because Telegram
limits inline Web App buttons to private chats. The server binds that token to
the authenticated user and checks current group membership before creation.
Clients cannot submit arbitrary destinations. Existing reminders remain visible
and editable only by their author, across all their chats.

The listener accepts JSON on the same origin, bounds request size, concurrency
and duration, disables access logging, and sends restrictive content/security
headers. Telemetry records a fixed web operation and validated user ID; it must
never include headers, query strings, launch tokens, names, text or request
bodies. Invalid sessions receive a reopen instruction.

## API and retries

| Route | Behavior |
| --- | --- |
| `GET /api/session` | Verified user and authorized creation context. Optional signed `launch`. |
| `GET /api/reminders` | Owner-only, key-ordered pages; `after` cursor and bounded `limit`. |
| `GET /api/reminders/{key}` | One owner-scoped record. |
| `POST /api/reminders` | Frozen UUID `request_id`, `text`, `schedule`, optional timezone, recurrence and signed `launch`. |
| `POST /api/reminders/{key}/reschedule` | Exact `etag`, schedule and optional timezone, replacement text or recurrence. Omitted recurrence is preserved; null removes it. |
| `POST /api/reminders/{key}/cancel` or `/retry` | Exact `etag`; retry is an explicit delivery decision. |
| `GET /api/community` | Signed context, fresh membership/admin access and personal preferences. |
| `GET/PATCH /api/preferences` | Owner-only timezone, exact nullable `etag` for changes. |
| `GET/PATCH /api/chats/{chat_id}/settings` | Launch-matched chat; current administrator required to change known flags with an exact revision. |
| `GET/POST /api/reposts` | Administrator-only targets in the signed topic; creation has a frozen UUID. |
| `PATCH /api/reposts/{key}` | Administrator-only conditional edit/archive; source, destination and cursor are immutable. |
| `POST /api/reposts/preview` | Bounded read-only VK preview; never publication. |
| `GET /api/chats/{chat_id}/games` | Current member; `kind=chess`, `geoguess` or `chess_play`; bounded summary, no active answers. |
| `GET /api/chats/{chat_id}/reactions` | Current member; whole-chat summary for `days=1`, `7` or `30`. |

Chat-scoped routes and repost routes accept the signed `launch` query argument.
Opening `/app` in another chat/topic changes that context. Shared access requires
the bot to be an administrator because Telegram only guarantees other-user
membership checks in that case. See [community tools](community-tools.md).

Records return a key, revision, timestamps and reminder payload. Creation keeps
schedule and text separate; text beginning with a duration cannot change the
deadline. Timezone conversion belongs to the server, which rejects nonexistent
or ambiguous local times. Lists expose delivery uncertainty rather than
pretending every failed response means a failed send.

An uncertain create retries its exact UUID and payload. A stale revision returns
409; preserve the draft, show current data and require another deliberate save.
Do not automatically apply stale edits. New web creations make one best-effort
Telegram confirmation with quick controls; losing that confirmation does not
lose the reminder or trigger another send.

## Hosting

Set `HUB_WEB_APP_URL` to an HTTPS origin and `HUB_WEB_PORT` to the internal
listener port (8081 by default). Empty URL disables the listener. Docker builds
the locked frontend dependencies in a separate Node stage and packages only
static assets into the Python application. Node is absent from the runtime.

Attach the bot and HTTPS proxy to the dedicated external `msu_hub_web` network;
do not publish the listener port on the host or attach the proxy to the database
network. Route the configured hostname to `http://msu_hub_bot:8081`, terminate
TLS at the proxy and redirect HTTP to HTTPS. Persist both network attachments
in deployment configuration. Keep proxy access logging disabled for this route
or redact query strings and authorization headers. Startup checks storage before
opening the listener; shutdown stops admission and drains requests before
closing database clients.

GitHub's production `HUB_WEB_APP_URL` variable supplies the origin. Follow
[deployment operations](deployment.md) when installing the host wrapper and
[database operations](database-operations.md) before changing storage contracts.

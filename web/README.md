# MSU Hub Mini App

The Telegram Mini App uses a shared React shell and isolated tool modules. `features/reminders/` owns reminder forms and delivery actions; `features/community/` owns paused repost sources, chat settings, game rankings/history and reaction leaderboards. `platform/api.ts` owns authenticated transport and reminder decoding; `platform/community.ts` owns community API decoding; `platform/telegram.ts` owns the Telegram bridge. Components make no database calls.

Use the Node LTS version supported by `package.json`:

```sh
npm ci
npm run typecheck
npm test
npm run format:check
npm run build
```

The server serves `dist/` and `/api/` on the same HTTPS origin. The development server binds to loopback; open through Telegram with a backend for authenticated development. Ordinary browser visitors see a link to the bot. There is no fake-data mode or client-side authentication bypass.

Telegram init data is sent only as `Authorization: tma …`, with no cookies or browser storage. A server-issued `launch` query token selects the chat/topic for reminders and community tools. Open `/app` in another chat/topic to change context; the client cannot enumerate or nominate other destinations. Existing destinations cannot be edited. Labels come from the server, and user content is rendered as plain text.

Creation freezes a UUID and payload before sending. A lost or ambiguous response keeps the form intact and retries that exact request. Validation errors permit edits. Existing-item mutations send the last observed `etag`; conflicts show the new record beside the preserved draft before another save. Telegram delivery marked uncertain requires an explicit warning and confirmation before retrying.

Lists paginate explicitly and retain user drafts during refresh. Mutations invalidate outstanding older list requests. Timezone-aware absolute schedules are resolved by the server, including daylight-saving ambiguity; the browser never converts a local date to the device timezone.

Tests block unmocked network access and cover request privacy, guarded mutations, unknown create outcomes, stale edits, pagination and timezone boundaries. Keep tools independently composed through the shell. Visited tools remain mounted but hidden so switching tools preserves drafts without browser storage.

## Community tools

Shared mutations require current Telegram administrator rights; rankings and reactions require membership. UI visibility is only a convenience: the server independently enforces authorization on every request. Existing source configuration is retained on pause or archive; there is no client control for enabling automatic publication. Preview reads a bounded sample and applies filters without sending Telegram messages. Create requests freeze a UUID and payload, and source edits use the same revision-review contract as reminders.

Daily/weekly reminders follow the selected timezone's wall clock; interval reminders follow elapsed minutes. The server schedules repeats, coalesces missed occurrences and pauses an uncertain series for explicit review. Cancelling a repeated reminder cancels all future occurrences. A saved personal timezone supplies new drafts without changing existing reminders or partially written drafts.

Chat settings apply to the whole chat, including its topics. Game history and reaction coverage/retention notes come from the server; rankings never infer scores from UI state. External preview links are limited to HTTPS VK hosts, and all provider text is rendered without HTML.

# MSU Hub Mini App

The Telegram Mini App uses a shared React shell and isolated tool modules. Reminders are the first tool. `platform/api.ts` owns authenticated requests and response validation; `platform/telegram.ts` owns the Telegram bridge; `features/reminders/` owns forms, dates, lists and actions. Components make no database calls.

Use the Node LTS version supported by `package.json`:

```sh
npm ci
npm run typecheck
npm test
npm run format:check
npm run build
```

The server serves `dist/` and `/api/` on the same HTTPS origin. The development server binds to loopback; open through Telegram with a backend for authenticated development. Ordinary browser visitors see a link to the bot. There is no fake-data mode or client-side authentication bypass.

Telegram init data is sent only as `Authorization: tma …`, with no cookies or browser storage. A server-issued `launch` query token selects the destination for new reminders. Existing destinations cannot be edited. Labels come from the server, and user content is rendered as plain text.

Creation freezes a UUID and payload before sending. A lost or ambiguous response keeps the form intact and retries that exact request. Validation errors permit edits. Existing-item mutations send the last observed `etag`; conflicts show the new record beside the preserved draft before another save. Telegram delivery marked uncertain requires an explicit warning and confirmation before retrying.

Lists paginate explicitly and retain user drafts during refresh. Mutations invalidate outstanding older list requests. Timezone-aware absolute schedules are resolved by the server, including daylight-saving ambiguity; the browser never converts a local date to the device timezone.

Tests block unmocked network access and cover request privacy, guarded mutations, unknown create outcomes, stale edits, pagination and timezone boundaries. Keep future tools independently composed through the shell; add navigation only when the tool exists.

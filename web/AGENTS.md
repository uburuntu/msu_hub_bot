# Mini App

- `app/` owns the shell; `features/` owns tools; `platform/` owns Telegram and authenticated API boundaries. Keep new tools independently composed.
- Send Telegram init data only in the authorization header. Never persist credentials, reminder text or server launch tokens in browser storage or analytics.
- Preserve drafts across refreshes and errors. An uncertain create retries its frozen request ID and payload; stale edits need explicit review against the latest version.
- Keep destinations server-authorized, Russian copy natural, controls keyboard-accessible and layouts usable inside small Telegram webviews.
- Run the package scripts for tests, types, formatting and production build. Test fixtures use synthetic identities and block unmocked fetches.

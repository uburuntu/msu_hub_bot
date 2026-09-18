# Mini App backend

- Verify Telegram init data on every API request; client IDs, destinations and initDataUnsafe are not authority.
- Keep credentials, init data, reminder text and request bodies out of logs. Serve static assets separately from authenticated JSON.
- Feature services own changes; HTTP adapters validate inputs and enforce owner scope, timeouts and concurrency guards.
- App lifecycle owns the listener. Stop admission and drain HTTP work before closing feature clients.

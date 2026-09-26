# TeleForge features

- Keep Telegram entrypoints, cards and named steps together; reuse bot services and presentation helpers instead of duplicating business rules.
- Constructor dependencies live as long as the feature. Request state belongs in typed contexts or application-owned persisted records.
- The host owns its router order, middleware, storage and transactions. Build isolated feature routers for dispatch tests; changing production composition requires deliberate replacement of the corresponding legacy routes.
- Preserve aliases, reply targeting, topic scope, Russian copy, authorization and exact-preview barriers. Native Telegram escape hatches are appropriate when their semantics matter.
- Use the local command decorator for one alias source and `format_input_error` for Russian acquisition guidance. Embedded handlers use `HubIsolationBridge` with the host's existing state middleware.
- Use platform delivery helpers directly from existing services when a Feature would only forward calls. Quiz services retain transactional jobs, deadlines and settlement; the host retains worker ownership.

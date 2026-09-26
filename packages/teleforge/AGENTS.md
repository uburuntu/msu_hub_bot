# TeleForge

- Build for feature authors: one feature owns related entrypoints, rendering and behavior. Plain helpers stay plain Python; an abstraction needs a distinct responsibility and a tested consumer.
- `App` owns application/resource lifetime, `Feature` owns shared configuration and services, and explicit typed contexts own invocations. Never put the current actor, update or context on a shared feature instance.
- Keep the package independent of its consumers. No imports from bots, provider-specific policy, database choice, environment loading or network work at import time.
- Use native aiogram routing and request middleware. Compile inherited declarations after class creation; preserve signatures, stable identities and deterministic order. An override retains its declaration unless explicitly replaced or disabled.
- Every parameter has one compiled source; middleware cannot override command arguments or callback fields. Stable action keys belong to the protocol, not Python method names.
- Keep actor, input source and delivery target separate. Callback acknowledgement, domain mutation and rendering are separate outcomes; never retry a committed action because its UI update failed.
- Record handler-return, acknowledgement and presentation facts independently; none proves a database commit. Keep error guidance localizable and configuration failures out of user copy.
- Application adapters own persistence/transactions. Jobs must preserve an application's atomic state-plus-enqueue contract. A process-local lock is not durable concurrency control.
- Documentation and examples are executable contracts for coding agents. Maintain concise guides, actionable diagnostics and offline pytest fixtures; avoid a second registry or testing language.
- Validate complete consumer journeys under native middleware, including jobs without fake events and failure recovery; short protocol stubs do not prove adoption.
- Verify framework details in the workspace's ignored upstream references. Run Ruff, strict mypy, package tests and wheel checks; include tests for uncertain delivery, cancellation, inherited routes and cross-feature isolation.
- For cross-bot/framework comparisons, inspect `references/derp` and `references/grammy`. If absent, clone with `gh repo clone uburuntu/derp references/derp` or `gh repo clone grammyjs/grammY references/grammy -- --depth=1`; preserve local reference changes. Use actual source to settle behavior.

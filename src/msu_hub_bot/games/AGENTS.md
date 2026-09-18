# Games

- Keep accepted actions, question snapshots and recovery deadlines in the feature store; memory holds only locks and presentation caches.
- Shared lifecycle code owns voting, expiry, ordered settlement and message repair. Definitions own providers and presentation.
- Never retry an uncertain initial Telegram send blindly. Preserve the callback token, exact answer order and attribution across restarts.
- Freeze score day and votes before settlement; Redis replay markers expire, and daily score floors make ordering significant.

# Games

- Keep questions, votes, deadlines and daily scores in the feature store; memory holds only locks and presentation caches.
- Shared quiz lifecycle code owns voting, expiry, ordered settlement and message repair. Definitions own providers and presentation.
- `chess_play/` owns human matches: pure chess rules, typed records and a service using the shared worker. Commit Elo changes with the terminal match; release the chat independently of Telegram delivery.
- Never retry an uncertain initial Telegram send blindly. Preserve the callback token, exact answer order and attribution across restarts.
- Freeze score day and votes before ordered settlement; commit score batches and round progress atomically. Permanent scores outlive round cleanup.

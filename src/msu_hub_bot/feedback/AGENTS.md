# Feedback

- `service.py` owns consent revisions, persistence and delivery; `context.py` captures bounded evidence; `presentation.py` renders the exact reviewed snapshot.
- Submit only the author's acknowledged preview. Copy selected fields explicitly; draft extras and unchecked context must never enter permanent reports.
- Keep the delivery destination server-configured. Unknown Telegram outcomes require review, not an automatic resend.
- The inbox is bot-scoped: check authors explicitly on draft operations and reviewers before inbox reads. Review metadata has its own revision; never couple it to notification delivery.
- Administrator notifications include only the bounded description and author. Selected context belongs in the authenticated review page.
- See [feedback](../../../docs/feedback.md) for retention, diagnostics coverage and operating contracts.

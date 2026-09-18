# Reminders

- Keep author and destination checks in the service so commands and web clients share the same policy.
- Commit the sending marker before Telegram; uncertain delivery requires an explicit owner retry.
- Pending reminders do not expire. Terminal cleanup cancels dependent work before deleting text.
- Preserve UTC deadlines and the chosen IANA zone; reject ambiguous or nonexistent local times.

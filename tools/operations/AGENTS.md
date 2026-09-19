# Operational checks

- Use administrative access for private queue diagnosis; never add broad runtime table grants or dump feature payloads into logs.
- Monitoring must not mutate data, prune backups, restart services or acknowledge a notification that failed.
- Repair through the owning feature service. A held external send is uncertain, not automatically retryable; never bulk-reset leases or generations.
- Keep host paths and notification destinations in private operator configuration. Public tools contain only generic checks and sanitized messages.

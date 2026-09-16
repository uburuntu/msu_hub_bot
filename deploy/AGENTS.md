# Deployment

- `deploy.py` is the restricted host-side release/rollback wrapper, not a general remote shell or database migration runner.
- Preserve private image transfer, immutable-image validation, host locking, preflight and one-poller cutover/rollback behavior.
- Runtime settings stay outside checkouts in private release files; never log expanded configuration or include it in an image.
- Shared infrastructure belongs to separate operational work. Do not delete/recreate database services or combine data migration with an application release.
- Follow [deployment operations](../docs/deployment.md). Data cutovers need separate reconciliation and recovery procedures; application rollback alone cannot reverse new database writes.

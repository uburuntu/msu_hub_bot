# Deployment

- [build_release.py](build_release.py) builds images with a layer cache, [image_smoke.py](image_smoke.py) validates them, and [client.py](client.py) pushes changed layers through an SSH forward to the host registry ([registry.compose.yml](registry.compose.yml)) before requesting the release. [host.py](host.py) is installed as `~/msu_hub_bot/deploy.py`, the restricted release/rollback wrapper.
- Preserve the loopback-only registry boundary, digest-pinned releases, immutable-image validation, host locking, preflight and one-poller cutover/rollback behavior.
- Storage compatibility includes the backend and Supabase API schema. Preserve the transition guard across interrupted namespace changes; recovery uses a proven image with compatible immutable configuration.
- Runtime settings stay outside checkouts in private release files; never log expanded configuration or include it in an image.
- Shared infrastructure belongs to separate operational work. Do not delete/recreate database services or combine data migration with an application release.
- Follow [deployment operations](../../docs/deployment.md). Data cutovers need separate reconciliation and recovery procedures; application rollback alone cannot reverse new database writes.

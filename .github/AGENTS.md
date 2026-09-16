# GitHub automation

- Workflows currently validate changes and deploy main through private SSH image transfer; they do not publish images to a registry.
- Keep action revisions/tool versions pinned, permissions minimal and production credentials confined to deployment jobs.
- Preserve locked uv checks, Ruff, tests, secret/workflow validation and Linux image smoke checks when updating the runtime matrix.
- Application deployment must not provision shared infrastructure or apply destructive schema migrations. Treat infrastructure/data cutovers as separate reviewed operations.

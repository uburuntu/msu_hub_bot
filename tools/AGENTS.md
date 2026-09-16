# Build and maintenance tools

- Tools own reproducible builds, image checks, deployment transport and redacted secret scanning; they should not import configured bot services.
- Keep subprocess arguments explicit, failures actionable and temporary artifacts private. Never print credential values or expanded environment configuration.
- Preserve Linux amd64 native coverage, non-root runtime checks and private archive transfer; macOS is a developer platform, not proof of production native compatibility.
- For migration work, update tool/runtime pins with evidence and keep CI, Docker and project settings consistent. No automatic production data migration belongs here.

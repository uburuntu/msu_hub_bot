# Observability tools

- Keep dashboard definitions as reproducible Logfire API create payloads; use `PROJECT_NAME` in metadata and keep deployment-specific copies outside Git.
- Follow [the observability contract](../../docs/observability.md). Never commit tokens, project UUIDs, operational URLs or captured query results; management credentials stay outside the bot runtime.
- Use unsampled counters for totals. Label alias activity and record-based latency as sampled; failure records and metrics can still be lost during export.
- Keep SQL explicitly scoped by service and environment, bound detail tables, and preserve trace/span columns for investigation links. Personal identifiers never become metric dimensions.
- Dimensioned time-series queries need both `groupBy` and `metrics` in the query plugin. Aggregate bar/pie queries use one string category and one numeric value column.
- Verify API and panel options against current Logfire documentation and implementation. Preserve dashboard versions on updates and validate both query meaning and rendered output.

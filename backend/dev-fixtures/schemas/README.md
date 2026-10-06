# Dev schema fixtures (AUTH_PROVIDER=dev only)

Stand-ins for Power BI's `GetSemanticModelSchema` output while there is no tenant access.

- `<dataset id>.json`: the schema every dev user sees.
- `<dataset id>.<object id>.json`: overrides it for one dev user, to simulate object-level security
  (e.g. `sales-ds.user-b.json` hides `Customer[CreditLimit]` from `user-b`).

Index them with: `uv run python -m app.jobs.metadata load-fixture --dataset-id sales-ds --file dev-fixtures/schemas/sales-ds.json`

The field names are a best guess at Fabric IQ's format. Replace them with real captured payloads after spike S3.

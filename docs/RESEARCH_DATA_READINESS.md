# QR-15 research-data readiness and import

QR-15 validates a user-supplied, licensed research-data package before QR-14 can consume it. A
package contains a manifest plus two checksummed CSV files: corporate-action-adjusted daily prices
with availability timestamps, and exact-date point-in-time universe membership including delisted
securities.

The importer confines every file to the project directory, verifies declared SHA-256 checksums,
parses strict schemas, rejects duplicate or unordered observations, requires membership for every
price date, and enforces minimum span, instrument count, per-instrument history, and eligible daily
breadth. A successful package receives a deterministic `READY_FOR_QR14` receipt.

Validate the policy without reading data:

```powershell
uv run pq research-data-readiness-check
```

Import an explicitly obtained package:

```powershell
uv run pq research-data-package-import --manifest path\to\package.yaml
```

QR-15 does not download data or accept a provider assertion without checksummed files. It cannot
access or consume the final holdout, route orders, or modify WP-14 evidence. Data licensing and the
truthfulness of vendor metadata remain the operator's responsibility.

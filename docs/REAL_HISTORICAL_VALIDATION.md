# QR-14 real historical validation runner

QR-14 converts corporate-action-adjusted daily bars and exact-date QR-01 universe membership into
the fixed QR-08 feature schema. It calculates 20-observation momentum, five-observation reversal,
20-observation realized volatility, and cross-sectional dollar-volume rank, then runs QR-08 through
QR-13 without changing any acceptance threshold.

The adapter rejects unadjusted prices, duplicate or unordered bars, missing universe observations,
insufficient history, and insufficient cross-sectional breadth. Every daily input carries an
availability timestamp and source-manifest SHA-256. A successful QR-11 result can create a QR-13
dossier; a failed result is recorded as `VALIDATION_REJECTED`.

```powershell
uv run pq research-real-validation-check
```

The current local INFY minute-session archive is not sufficient input for QR-14. Execution requires
adjusted daily history for a multi-stock universe and an exact-date point-in-time universe artifact.
The check command validates contracts only and does not download data, access or consume the final
holdout, route orders, or modify WP-14 evidence.

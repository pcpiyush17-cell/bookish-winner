# QR-11 model-selection stability gate

QR-11 decides whether QR-10 has earned continued validation work relative to QR-09. It accepts
only matching dataset fingerprints, model identities, fold identities, validation samples, and
cost cases.

The gate requires minimum mean RMSE improvement, positive information coefficient across enough
folds, a bounded share of fold-level RMSE degradation, minimum mean information coefficient, and
non-negative improvement in every cost case. Failure retains ridge as the simpler baseline and
records stable machine-readable reasons.

A pass produces `BOOSTED_VALIDATION_CANDIDATE`, not approval. The result has a deterministic
SHA-256 fingerprint and still cannot access the final holdout or operational promotion path.

```powershell
uv run pq research-model-stability-check
```

QR-11 cannot fit models, alter results, route orders, or modify WP-14 evidence.

# QR-08 leakage-safe ML dataset foundation

QR-08 creates supervised-learning samples and evaluation folds before any machine-learning model is
allowed into the research lab. It binds point-in-time features to forward returns beginning one
observation after the signal.

The builder enforces an exact versioned feature schema, point-in-time eligibility, chronological
ordering, deterministic sample identifiers, and a canonical SHA-256 fingerprint. Its expanding
walk-forward folds keep validation strictly after training, purge enough observations to cover the
execution lag and label horizon, and insert an embargo before the next validation fold. A training
label must finish before its validation window begins.

The default feature names are a schema contract only; QR-08 does not calculate features, fit a
model, optimize hyperparameters, inspect the final holdout, or claim predictive power.

```powershell
uv run pq research-ml-dataset-check
```

QR-08 cannot route orders, approve a model, or modify WP-14 evidence.

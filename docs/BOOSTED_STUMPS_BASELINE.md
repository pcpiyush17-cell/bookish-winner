# QR-10 deterministic boosted-stumps baseline

QR-10 tests whether shallow nonlinearity adds value beyond QR-09. It fits gradient-boosted
one-split trees to each QR-08 training fold using fixed estimator count, shrinkage, minimum leaf
size, bounded threshold candidates, deterministic tie-breaking, and clipped predictions.

Validation reports RMSE, training-mean RMSE, information coefficient, directional accuracy, and
non-compounded cost-stressed forward-return events. A same-dataset comparison API measures RMSE,
information-coefficient, and cost-case deltas against the QR-09 ridge result. Complexity is not
treated as progress unless it beats the linear baseline consistently.

```powershell
uv run pq research-boosted-stumps-check
```

QR-10 does not tune on validation, inspect the final holdout, create a deployable P&L curve, route
orders, or alter WP-14 evidence.

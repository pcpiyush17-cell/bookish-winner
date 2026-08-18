# QR-12 model-evaluation workflow

QR-12 runs the fixed QR-09 ridge baseline and QR-10 boosted-stumps baseline on the same QR-08
validation dataset, then applies the QR-11 stability gate. It produces one deterministic report
containing dataset, model, stability-decision, cost-case, and SHA-256 identities.

The workflow validates all component identities, feature sets, cost cases, and safety boundaries
before fitting. Reports are stored under their dataset and report fingerprints and cannot silently
overwrite conflicting evidence.

```powershell
uv run pq research-model-evaluation-check
```

This command validates the orchestration contract only. Execution requires an explicitly supplied
QR-08 `MLDatasetResult`; QR-12 cannot discover or inspect the final holdout. A stability pass means
only `BOOSTED_VALIDATION_CANDIDATE`, never approval, operational promotion, or permission to route
orders. QR-12 does not modify WP-14 evidence.

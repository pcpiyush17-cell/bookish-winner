# QR-13 candidate freeze and holdout readiness

QR-13 converts a verified, successful QR-12 validation result into an immutable candidate dossier.
It freezes the dataset and report fingerprints, model and stability identities, Git commit, `uv.lock`,
component configurations, and the shared transaction-cost contract.

Only an evaluation with the QR-11 decision `BOOSTED_VALIDATION_CANDIDATE` and no failure reasons can
receive `HOLDOUT_READY`. The QR-12 report fingerprint is independently recomputed before freezing.
Artifacts must be inside the project and are represented by paths plus SHA-256 checksums. Dossiers
are fingerprint-addressed and cannot silently overwrite conflicting content.

```powershell
uv run pq research-candidate-freeze-check
```

`HOLDOUT_READY` is authorization readiness, not holdout access. QR-13 cannot inspect or consume the
QR-02 final holdout, approve operational promotion, route orders, or modify WP-14 evidence.

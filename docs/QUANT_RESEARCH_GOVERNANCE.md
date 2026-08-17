# QR-00 quantitative research governance

The quantitative research lab runs in parallel with WP-14 but has no authority over paper,
shadow, or production execution. Its purpose is to reject fragile ideas through reproducible,
cost-aware experiments before any strategy can enter the operational promotion gates.

## Isolation contract

- Research source artifacts live under `research/`; mutable research state lives under
  `state/research/`.
- Research must not read from or write to operational credentials, WP-14 recordings, evidence
  databases, or WP-14 backups as experimental inputs or outputs.
- Research code cannot enable production order routing.
- WP-14 strategy, configuration, wallet, session counts, and reports remain frozen and separate.
- A research result cannot promote itself. Promotion requires a later, explicit work package and
  human approval after all blueprint gates pass.

The machine-readable boundaries are versioned in
[`config/research/governance_v1.yaml`](../config/research/governance_v1.yaml).

## Experiment contract

Every experiment begins with an immutable manifest containing:

- a falsifiable hypothesis and strategy family;
- point-in-time universe and dataset manifests with SHA-256 checksums;
- exact Git commit, dependency-lock hash, and configuration hash;
- disjoint train, validation, and untouched holdout windows;
- mandatory 1.0x, 1.5x, and 2.0x transaction-cost cases;
- explicit WP-14 isolation and disabled production routing.

`DRAFT`, `EVALUATED`, and `REJECTED` are the only QR-00 statuses. There is deliberately no
`APPROVED` status. See
[`config/research/experiment.example.yaml`](../config/research/experiment.example.yaml) for the
schema shape.

## Research sequence

1. QR-01 builds a point-in-time, survivorship-safe NSE universe and data-quality layer. Its
   exact-observation limits are documented in
   [`POINT_IN_TIME_UNIVERSE.md`](POINT_IN_TIME_UNIVERSE.md).
2. QR-02 adds immutable experiment/result storage and leakage-safe evaluation utilities, described
   in [`RESEARCH_EXPERIMENTS.md`](RESEARCH_EXPERIMENTS.md).
3. QR-03 establishes the simple controls in
   [`RESEARCH_BENCHMARKS.md`](RESEARCH_BENCHMARKS.md) before complex models are attempted.
4. Challenger strategies progress from statistical baselines to machine learning and only then
   deep learning when data volume and benchmark evidence justify it.

QR-04 begins that progression with the interpretable, cost-aware
[`cross-sectional momentum challenger`](CROSS_SECTIONAL_MOMENTUM.md).

QR-05 adds the complementary
[`regime-aware mean-reversion challenger`](REGIME_MEAN_REVERSION.md).

QR-06 adds the strategy-agnostic, unlevered
[`volatility-targeting risk overlay`](VOLATILITY_TARGETING.md).

QR-07 adds the long-only
[`correlation-aware strategy allocator`](CORRELATION_AWARE_ALLOCATION.md).

QR-08 establishes the
[`purged walk-forward ML dataset foundation`](PURGED_WALK_FORWARD_ML.md).

No QR work changes the ongoing WP-14 evidence run.

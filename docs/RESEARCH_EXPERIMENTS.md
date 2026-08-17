# QR-02 experiment registry and leakage-safe evaluation

QR-02 stores experimental intent and evaluation results as immutable, checksummed research
artifacts. Registration verifies the clean Git commit, `uv.lock`, strategy configuration,
point-in-time universe manifest, and every dataset manifest before copying a canonical experiment
record into `research/results/experiments/`.

Evaluation obeys these rules:

- timestamps use half-open, disjoint train, validation, and holdout windows;
- walk-forward validation uses anchored training plus explicit purge and embargo gaps;
- model or parameter selection is declared as validation-only;
- the final holdout is claimed exactly once in `state/research/holdout/`;
- every split reports 1.0x, 1.5x, and 2.0x transaction-cost cases;
- annualized metrics cannot be headlined for fewer than 252 trading days;
- results may be `REJECTED` or `CANDIDATE`, but cannot approve operational promotion.

Research results remain isolated from WP-14 state, recordings, evidence, and backups. Production
order routing is structurally disabled by the result schema.

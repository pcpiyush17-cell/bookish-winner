# QR-07 correlation-aware strategy allocation

QR-07 combines validated strategy return streams; it does not create a new stock-selection signal.
It starts with inverse-volatility scores and penalizes strategies whose trailing returns are
positively correlated with the rest of the eligible set.

The allocator uses point-in-time availability and trailing returns only. Decisions execute one
observation later. Long-only weights are capped per strategy, cash is allowed while turnover ramps
within its limit, and an unavailable strategy is removed immediately. A rebalance threshold avoids
small cost-generating changes.

Every validation result reports 1.0x, 1.5x, and 2.0x cost cases, turnover, drawdown, and excess
return versus an equal-weight allocation over the identical eligible strategies and execution
window. The method is an interpretable research baseline, not a claim of optimality.

```powershell
uv run pq research-portfolio-allocation-check
```

QR-07 cannot route orders, approve a strategy, inspect the QR-02 holdout, or modify WP-14 evidence.

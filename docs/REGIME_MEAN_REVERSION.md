# QR-05 regime-aware mean-reversion challenger

QR-05 tests whether recent cross-sectional losers revert over the next observation after realistic
costs. It is a research challenger, not a profitability claim.

The strategy calculates short-horizon return z-scores only for instruments with complete
point-in-time membership, prices, and minimum dollar volume. It enters sufficiently negative
z-scores and retains positions until a less-negative exit threshold, reducing threshold churn.
Signals execute one observation later.

A trailing equal-weight market return classifies each signal as range/normal, trending, or high
volatility. New risk is allowed only in the range/normal regime. Trending and high-volatility
signals explicitly target cash. Instruments leaving the universe are liquidated immediately when
an observable exit price exists.

Every result includes 1.0x, 1.5x, and 2.0x cost cases, turnover, and drawdown. Challenger comparison
uses validation results only. QR-02 continues to protect the final holdout.

```powershell
uv run pq research-mean-reversion-check
```

The module cannot route orders, approve promotion, or modify WP-14 evidence.

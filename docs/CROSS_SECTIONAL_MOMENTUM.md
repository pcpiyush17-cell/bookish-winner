# QR-04 cross-sectional momentum challenger

QR-04 is the first challenger to the QR-03 controls. It is a long-only, point-in-time
cross-sectional momentum design—not an assertion of profitability.

At each month end, the strategy ranks instruments that remained eligible throughout the full
signal window by their lagged return. The most recent observations are skipped to avoid conflating
short-term reversal with medium-term momentum. Decisions execute on the next observation, never on
the signal bar. The selected top fraction is sized by inverse realized volatility, subject to a
weight cap, and a rank buffer retains existing positions to reduce unnecessary turnover.

Every simulation includes liquidation and one-way trading costs at 1.0x, 1.5x, and 2.0x. Missing
history, insufficient point-in-time membership, unavailable exit prices, invalid ordering, and
capital exhausted by costs fail closed. Comparison uses QR-03 validation results only; the final
holdout remains governed by QR-02 and cannot be tuned repeatedly.

Validate the strategy contract without running an experiment:

```powershell
uv run pq research-momentum-check
```

This module does not place orders, approve promotion, or modify WP-14 evidence.

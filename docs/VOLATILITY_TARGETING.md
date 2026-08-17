# QR-06 volatility-targeting overlay

QR-06 adds a risk overlay, not a new source of alpha. It scales a validated strategy return stream
toward a 10% annualized volatility target using trailing realized volatility only.

The overlay is deliberately conservative for the virtual wallet:

- exposure is bounded between cash and 1x, so it cannot introduce leverage or short exposure;
- every exposure decision executes one observation after its signal;
- a volatility floor prevents unstable division by near-zero estimates;
- maximum step and rebalance-threshold controls damp exposure churn;
- every result reports turnover, drawdown, realized volatility, and 1.0x, 1.5x, and 2.0x costs;
- validation results are compared with static full exposure to show whether scaling actually helps.

```powershell
uv run pq research-volatility-targeting-check
```

The overlay cannot route orders, approve promotion, inspect the QR-02 holdout, or alter WP-14
evidence.

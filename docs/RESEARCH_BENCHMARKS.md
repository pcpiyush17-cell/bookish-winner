# QR-03 research benchmark controls

QR-03 establishes simple controls that every later statistical, machine-learning, or deep-learning
challenger must beat. Complexity is not evidence of an edge.

The versioned suite contains cash, point-in-time equal-weight buy-and-hold, monthly equal-weight
rebalancing, and daily equal-weight rebalancing. Membership changes force a rebalance using only the
membership and prices supplied at that timestamp. A held constituent must have an observable exit
price; missing transition prices fail closed.

All invested controls use fractional research units and apply explicit one-way transaction costs at
1.0x, 1.5x, and 2.0x. Each result reports net return, maximum drawdown, and turnover for every cost
case. Challenger comparison accepts validation results only and tests against the strongest control
in every cost case. It cannot approve operational promotion or access WP-14 evidence.

Validate the versioned suite configuration with:

```powershell
uv run pq research-benchmarks-check
```

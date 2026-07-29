# Paper Session Evidence Protocol

WP-14 foundation completion does not satisfy the operational acceptance period by itself.
Promotion requires evidence from ten successful dry sessions followed by thirty successful
formal paper sessions.

## Successful session criteria

A session counts only when all of the following are persisted:

- pre-flight passed and the runtime reached `READY`;
- the single-instance lock was held for the session;
- feed health remained subject to the fresh-data gate;
- strategy signals passed through the risk engine and OMS without bypass;
- shutdown completed cleanly;
- broker and local cash/positions reconciled with no differences;
- no active kill switch remained at shutdown;
- state snapshots and the immutable daily report were written.

Interrupted, failed, unreconciled, or kill-switched sessions do not count. Formal sessions are
locked in code until ten successful dry sessions exist. The thirty-session formal target must
be collected over actual scheduled paper sessions; tests and accelerated simulations are not
substitutes for this operating evidence.

## Review cadence

After every session, review feed gaps, rejected risk reasons, order/fill lifecycle, positions,
cash, P&L, costs, report completeness, and shutdown status. Pause the sequence on any
reconciliation failure, investigate it, record resolution evidence, and restart the formal
count only according to the project’s promotion decision.

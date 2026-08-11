# Paper Session Evidence Protocol

WP-14 foundation completion does not satisfy the operational acceptance period by itself.
Revised hybrid acceptance requires five successful live dry sessions on distinct market dates and
thirty successful historical paper replay sessions on distinct historical market dates. The two
sources are stored and audited separately.

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

Interrupted, failed, unreconciled, or kill-switched sessions do not count. A live session must also
retain fresh-feed, authentication, account, calendar, and runtime evidence. A historical replay
must use one complete, gap-free, checksum-verified minute or 15-minute candle date and carry the
explicit `replay` evidence-source marker. Replay never proves WebSocket, reconnect, network,
wall-clock, sleep, current instrument-master, or daily authentication behavior.

The former ten-dry-plus-thirty-formal policy remains available as a conservative legacy workflow,
but it is no longer the WP-14 acceptance target. Neither tests nor ordinary backtests count toward
the revised hybrid gate.

## Review cadence

After every session, review feed gaps, rejected risk reasons, order/fill lifecycle, positions,
cash, P&L, costs, report completeness, and shutdown status. Pause the sequence on any
reconciliation failure, investigate it, record resolution evidence, and resume only according to
the project's promotion decision.

Use the combined read-only audit:

```powershell
uv run pq hybrid-evidence-status `
  --operational-path F:\Quant_Trader\state\trading.sqlite `
  --replay-path F:\Quant_Trader\state\replay\trading.sqlite
```

See the
[`WP-14 operational validation runbook`](runbooks/WP14_OPERATIONAL_VALIDATION.md) before attempting
the first countable session.

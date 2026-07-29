# WP-14 operational validation runbook

Status: **NOT READY TO START COUNTING SESSIONS**.

The lifecycle engine, paper broker, evidence schema, and immutable daily reports are implemented.
The production-authenticated current-data collector is not yet assembled into an operator-run
paper process. Until that gap is closed and verified, do not treat tests, replay, accelerated
clocks, manually inserted database rows, copied reports, or repeated runs on one market date as
operational evidence.

## Read-only status

Initialize the operational database once, then inspect its evidence without changing it:

```powershell
uv run pq storage-init --path F:\Quant_Trader\state\trading.sqlite
uv run pq paper-evidence-status --path F:\Quant_Trader\state\trading.sqlite
```

The status command never starts a runtime or writes evidence. It validates clean shutdown,
reconciliation, required pre-flight/bar/shutdown snapshots, report identity, and one countable
session of each evidence kind per market date. It will continue to show `PENDING` until the
requirements are genuinely met.

## Readiness work before session 1

- Wire the production-authenticated WebSocket collector to the paper runtime with no production
  broker order path.
- Add an operator start/stop command that uses `SystemClock`, the current NSE calendar and
  instrument snapshot, fresh-feed health, the approved strategy manifest, and the paper broker.
- Verify account identity and authentication are reads only; all order intents must terminate at
  the paper broker.
- Record the Git commit, release-manifest hash, strategy-manifest hash, and config fingerprint.
- Rehearse WebSocket reconnect, graceful shutdown, kill switch, database backup, and restart
  recovery.
- Review and approve disk paths on `F:` so runtime data does not consume the `C:` drive.

## Evidence sequence

1. Run exactly one scheduled dry session on a market date.
2. After shutdown, review feed gaps, signals, risk decisions, orders, fills, positions, cash,
   costs, P&L, reconciliation, snapshots, and the immutable report.
3. Resolve every anomaly before continuing. Failed or interrupted sessions never count.
4. Repeat until the auditor reports `10/10` dry sessions.
5. Change `evidence_kind` to `formal`; the runtime itself rejects formal sessions before the dry
   gate.
6. Collect and review 30 scheduled formal sessions, again at most one countable formal session per
   market date.
7. Treat any unresolved reconciliation failure or evidence-integrity issue as a stop condition.

## Per-session operator record

Copy this section into an external operator log for each session; do not place secrets or account
details in Git.

- Date and evidence kind:
- Git commit and approved manifests:
- Pre-flight result and any interrupted-session recovery:
- Feed start/freshness/reconnect notes:
- Risk rejection review:
- Order/fill lifecycle review:
- Cash, position, P&L, and cost review:
- Reconciliation result:
- Kill-switch state:
- Shutdown state and report path:
- Anomalies, owner, resolution, and approval:

Passing engineering tests prepares the workflow but does not satisfy operational acceptance.

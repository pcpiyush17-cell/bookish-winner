# WP-14 operational validation runbook

Status: **HYBRID EVIDENCE 1/5 LIVE DRY, 0/30 HISTORICAL REPLAY**.

The lifecycle engine, paper broker, evidence schema, immutable reports, and authenticated
current-data runner are implemented. The non-counting readiness rehearsal passed on 2026-07-30.
Tests, rehearsal data, replay, accelerated clocks, manually inserted database rows, copied reports,
or repeated runs on one market date are never operational evidence.

## Readiness rehearsal result

The isolated 2026-07-30 rehearsal ran for approximately 18 minutes 45 seconds from merged `dev`
commit `f1c68aed444474271386a511e951799156ac177f`. It accepted 1,468 Kite ticks, including 396
groups of distinct same-exchange-timestamp updates, with zero data-quality violations. The raw
recording contained 1,471 events and matched SHA-256
`09eabd5a3d64feed7a57f8938296abf4beda3442098c4f44c7350d744d05de48`.

The session used `PaperBroker` exclusively, completed five simulated fills, applied the versioned
delivery-cost model, reconciled cash and positions, wrote its immutable report, passed database
integrity, released its runtime lock, and shut down cleanly. Production order routing remained
unreachable. Its isolated database, reports, and recording are non-counting rehearsal artifacts
and must never be copied into operational evidence paths.

## Accepted operational evidence

### Dry Session 1 of 10 — 2026-07-30

The first countable dry session ran from merged `dev` commit
`d01c142d36e541ff6ddb4ad010e233d2a03dcbd5` for approximately 1 hour 49 minutes. The runtime
session ID was `5fc4d0fa-f7bf-423b-84eb-926aff314ba0`; the independently finalized recording ID
was `3196021e-95af-4a3b-a8b8-04c45e83a5f7`.

The recording contained 8,398 lifecycle and market-data events, including 8,395 accepted ticks
and 2,406 groups with repeated exchange timestamps. Every accepted tick had a unique event ID,
there were zero data-quality violations, and the Parquet file matched SHA-256
`dd9513e6648f4866c6d8deecf95fe7ab5c7e760fcbd471dedb1d369dd875bfbe`.

The runtime processed 84 signals, approved 10 risk decisions, rejected 74, and completed 10
simulated orders and fills with no open orders at shutdown. It ended with INR 8,792.85 cash, one
INFY share valued at INR 1,172.00, INR 9,964.85 net liquidation value, INR -0.80 realised P&L,
INR -1.90 unrealised P&L, and INR 32.45 in versioned estimated costs. The database and
post-session backup passed integrity checks; reconciliation was healthy, the kill switch was
inactive, the runtime lock was released, and production order routing remained unreachable.

The broker reported WebSocket close code 1006 during the operator-triggered closing handshake.
This did not interrupt operational processing: recording finalization, the immutable report,
reconciliation, clean runtime shutdown, and lock release all completed. The read-only evidence
legacy live auditor accepted the session. Under the revised hybrid policy it is Live Dry Session
`1/5`; it does not create historical replay credit.

## Read-only status

Initialize the operational database once, then inspect its evidence without changing it:

```powershell
uv run pq init-db --path F:\Quant_Trader\state\trading.sqlite
uv run pq paper-evidence-status --path F:\Quant_Trader\state\trading.sqlite
```

The status command never starts a runtime or writes evidence. It validates clean shutdown,
reconciliation, required pre-flight/bar/shutdown snapshots, report identity, and one countable
session of each evidence kind per market date. It will continue to show `PENDING` until the
requirements are genuinely met.

## Readiness rehearsal checklist

- Review `config/paper_runner.example.yaml`, update the dated instrument snapshot directory, and
  confirm every state/data/report path resolves under `F:\Quant_Trader`.
- Inspect the runner and confirm it constructs `PaperBroker`, not a production broker adapter.
- Verify account identity and authentication are reads only; all order intents must terminate at
  the paper broker.
- Confirm the paper runtime loads the dated delivery-cost configuration and explicit spread,
  slippage, and impact assumptions. Every paper fill must debit both simulated broker cash and the
  accounting ledger with versioned estimated components; DP charges are applied once per scrip per
  market date.
- Record the Git commit, release-manifest hash, strategy-manifest hash, and config fingerprint.
- Rehearse WebSocket reconnect, graceful shutdown, kill switch, database backup, and restart
  recovery.
- Review and approve disk paths on `F:` so runtime data does not consume the `C:` drive.
- Start only with the exact dry-session confirmation phrase:

  ```powershell
  uv run pq paper-session-start --config config/paper_runner.example.yaml `
    --confirm "START DRY PAPER SESSION"
  ```

- Use Ctrl+C to rehearse graceful shutdown, then verify the recording manifest, runtime report,
  released lock, database integrity, and evidence auditor output. Rehearsals do not count; the
  first operational dry session must be explicitly designated before it starts.

## Hybrid evidence sequence

1. Run at most one designated live dry session on a market date.
2. After shutdown, review feed gaps, signals, risk decisions, orders, fills, positions, cash,
   costs, P&L, reconciliation, snapshots, and the immutable report.
3. Resolve every anomaly before continuing. Failed or interrupted sessions never count.
4. Repeat until the hybrid auditor reports `5/5` live dry sessions.
5. Independently replay 30 distinct, complete, gap-free historical market dates through the
   historical paper runner. Store them only in `state/replay/trading.sqlite`.
6. Require immutable source manifests and matching curated-data SHA-256 checksums. A duplicate
   replay date, data gap, invalid candle, checksum mismatch, failed pre-flight, unclean shutdown,
   or reconciliation difference makes that replay ineligible.
7. Run `pq hybrid-evidence-status`; acceptance requires `5/5` live and `30/30` replay with no
   unresolved live evidence issue.
8. Treat any reconciliation failure or evidence-integrity issue as a stop condition.

Historical replay validates deterministic strategy, risk, simulated execution, costs, accounting,
and reporting. It cannot validate live authentication, feed freshness, WebSocket ordering,
reconnect behavior, network stability, wall-clock scheduling, or unattended PC operation; those
remain obligations of the five live sessions.

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

The callback and tick-field assumptions follow Zerodha's official
[Kite Connect WebSocket documentation](https://kite.trade/docs/connect/v3/websocket/) and
[official Python SDK callback example](https://kite.trade/docs/connect/v3/agent-setup/).

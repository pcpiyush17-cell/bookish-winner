# WP-14 operational validation runbook

Status: **RUNNER IMPLEMENTED; OPERATOR READINESS REHEARSAL PENDING**.

The lifecycle engine, paper broker, evidence schema, immutable reports, and authenticated
current-data runner are implemented. Do not begin counting sessions until the readiness rehearsal
below passes. Tests, replay, accelerated clocks, manually inserted database rows, copied reports,
or repeated runs on one market date are never operational evidence.

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

## Readiness rehearsal before session 1

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
  released lock, database integrity, and evidence auditor output. A rehearsal is not automatically
  countable; the operator must designate beforehand whether it is the first scheduled dry session.

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

The callback and tick-field assumptions follow Zerodha's official
[Kite Connect WebSocket documentation](https://kite.trade/docs/connect/v3/websocket/) and
[official Python SDK callback example](https://kite.trade/docs/connect/v3/agent-setup/).

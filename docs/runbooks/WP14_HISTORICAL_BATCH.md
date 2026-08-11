# WP-14 controlled historical replay batch

This Windows PowerShell workflow processes complete NSE market dates without requiring attendance
during market hours. It delegates every download and replay to the existing fail-closed `pq`
commands. It does not enable production order routing and does not make historical replay count as
live operational evidence.

## Safety properties

- Dates must be unique and strictly increasing.
- The repository must be on a clean `dev` branch so each session records a verifiable commit.
- Every date must be an NSE trading date with exactly 375 curated minute bars, zero invalid rows,
  and zero gaps.
- Each replay uses an immutable checksum-verified manifest and the isolated replay wallet database.
- The batch stops at the first error. It never skips a failed date or fabricates evidence.
- After every successful session, the database is integrity-checked, backed up without overwrite,
  and audited for the hybrid evidence count.
- Configurations and full command transcripts are retained under `state/replay/batches/<batch-id>`.

Because the replay wallet is cumulative, run dates later than the last accepted replay date. An
exact rerun of a downloaded date may reuse its immutable data manifest, but an already-counted
market date cannot create a second accepted session.

## Before each batch

1. Merge all approved code to `dev`, switch to `dev`, pull it, and confirm the tracked worktree is
   clean.
2. Refresh Kite authentication if the production access token has expired.
3. In the same PowerShell terminal, set `KITE_API_KEY` and `KITE_EXPECTED_USER_ID`. Never put the API
   key, secret, request token, or access token in this script or Git.
4. Use only completed market dates after the most recent accepted replay date.

## Current next-date batch

After the accepted 2026-07-29 Session 1, the completed dates available through 2026-08-11 are:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\run_historical_replay_batch.ps1 `
  -Dates 2026-07-30 2026-07-31 2026-08-03 2026-08-04 2026-08-05 2026-08-06 2026-08-07 2026-08-10 2026-08-11 `
  -Confirm "RUN CONTROLLED HISTORICAL REPLAY BATCH"
```

`ExecutionPolicy Bypass` applies only to this child PowerShell process; it does not change the
computer or user execution policy.

This creates Sessions 2 through 10 if every date passes. Later completed dates can be supplied to
the same command in chronological batches until the 30-session replay requirement is met.

If the instrument snapshot moves, pass its exact directory with `-SnapshotDirectory`. The durable
instrument remains `NSE:INFY` unless the approved WP-14 plan is explicitly revised.

## Failure handling

Do not delete or edit evidence after a failure. Read the last transcript in the printed batch
directory, correct the underlying problem, and start a new batch beginning with the failed date.
Successfully completed earlier dates remain counted and their post-session backups remain intact.

At any time, inspect the count without mutation:

```powershell
uv run pq hybrid-evidence-status --operational-path state/trading.sqlite `
  --replay-path state/replay/trading.sqlite
```

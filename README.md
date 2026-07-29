# Personal Quant Trading System

A local-first, safety-focused personal quant trading system built from the engineering
contract in [`PERSONAL_QUANT_SYSTEM_BLUEPRINT.md`](PERSONAL_QUANT_SYSTEM_BLUEPRINT.md).

The project has completed **WP-00 through WP-12**, including portfolio accounting, a
fail-closed pre-trade risk engine, a persistent paper-trading OMS, a deterministic
event-driven backtester, and a broker-independent baseline strategy. It has no production
broker adapter and cannot place, modify, or cancel real-money orders.

## Requirements

- Windows 11 or Linux
- Python 3.11.9, managed by `uv`
- Git

For this Windows workspace, `uv` and Python are installed under the ignored `.tools/`
directory on the F: drive. Add the project-local `uv` directory to the current PowerShell
session when needed:

```powershell
$env:Path = "F:\Quant_Trader\.tools\uv;$env:Path"
$env:UV_PYTHON_INSTALL_DIR = "F:\Quant_Trader\.tools\python"
```

## Set up

```powershell
uv sync --all-groups
uv run pq doctor
```

## Quality checks

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
```

Run every check with:

```powershell
uv run pre-commit run --all-files
```

## Configuration

[`config/base.example.yaml`](config/base.example.yaml) documents every current application
setting. Configuration models are immutable and strict: missing values, type coercion, and
unknown fields fail validation. Broker secrets are not configuration values; only the names
of environment variables that will hold those secrets are recorded.

Validated configuration exposes a deterministic SHA-256 fingerprint for run auditing. The
fingerprint is unaffected by YAML key ordering.

Domain code uses `Money` for minor-unit monetary values and injected `Clock` implementations
for time. Floats are rejected by `Money`, and application logic must receive a clock instead
of calling the wall clock directly.

## Operational database

Initialize or migrate the local SQLite database:

```powershell
uv run pq init-db
uv run pq db-check
uv run pq backup
```

The default database is `state/trading.sqlite`; verified online backups go to `backups/`.
Both locations are excluded from Git. SQLite connections use WAL mode, `synchronous=FULL`,
foreign-key enforcement, and a five-second busy timeout. Migrations are numbered SQL files
and their checksums are recorded, so an already-applied migration cannot be edited silently.

Repository writes run in explicit transactions. Runtime sessions survive clean restart, and
the foundational event log treats duplicate event IDs as harmless no-ops.

## Broker mock and Kite sandbox

Application code depends only on the broker-neutral protocol. `MockBroker` provides explicit,
deterministic fills for tests. `SandboxKiteAdapter` is fixed to Kite's sandbox root and LIMIT
orders; no production API root or production adapter exists.

Copy the variable names from `.env.example` into your local secret-management workflow. Do
not place values in the tracked example. To begin the human-mediated sandbox login flow:

```powershell
uv run pq kite-login
uv run pq kite-login --exchange
```

The first command prints the sandbox login URL. Complete login and two-factor authentication
in your browser. The second command prompts privately for the short-lived request token,
verifies the expected sandbox user, and stores the access token under the Git-ignored `state/`
directory. Browser credential automation is intentionally absent.

## Instrument master and market calendar

```powershell
uv run pq instruments-download
uv run pq instruments-validate --directory data/reference/instruments/provider=zerodha/date=YYYY-MM-DD
uv run pq calendar-check --date 2026-10-02
```

Snapshots are normalized, checksum-protected, and immutable within a date. Lookups use
durable keys such as `NSE:INFY`; broker tokens remain dated reference data and may change.
Local snapshots stay under the Git-ignored `data/reference/` directory.

The tracked 2026 NSE calendar records its verification date and official sources. Published
holidays and session windows are explicit. The announced Diwali Muhurat session remains
closed until NSE publishes its exact timings. Missing market data never infers a holiday.

## Historical candle ingestion

```powershell
uv run pq historical-download --instrument NSE:INFY --start 2026-01-01 --end 2026-07-28 `
  --interval day --snapshot data/reference/instruments/provider=zerodha/date=YYYY-MM-DD
```

The downloader is limited to two historical requests per second. Exact reruns reuse the
existing manifest without another broker call. Raw and curated data are separate immutable
Parquet layers; malformed batches are quarantined, and gaps are reported without forward
filling. Runtime market datasets remain excluded from Git.

## Analytics and features

`VerifiedDataset` accepts only checksum-verified curated manifests and requires an aware
`as_of` cutoff. It provides lazy Polars scans and an in-memory, read-only DuckDB query
context. The feature registry includes versioned one-bar return and three-bar moving-average
definitions; materialized Parquet outputs have checksum-protected manifests.

The no-lookahead and reproducibility rules are documented in
[`docs/POINT_IN_TIME_ANALYTICS.md`](docs/POINT_IN_TIME_ANALYTICS.md).

## Cost and break-even estimates

```powershell
uv run pq cost-estimate --quantity 100 --buy-price 100 --sell-price 110 `
  --spread-bps 10 --slippage-bps 5
uv run pq break-even --capital 100000 --variable-costs 100 --target-profit 1000
```

The tracked charge configuration records its calculation version, verification date, and
official sources. Estimates use `Decimal`, explicit paise rounding, one DP charge per sold
scrip lifecycle, and base/1.5x/2.0x execution-cost scenarios. Rates can change; review the
configuration against current broker and regulatory sources before any production use, and
reconcile estimates against contract notes when actual charge data becomes available.

## Portfolio accounting

Migration `0002` adds deduplicated fills, position projections, an append-only cash journal,
version-linked cost entries, and valuation snapshots. Broker fills atomically update cash and
long-only positions; identical fill replays are no-ops, while conflicting duplicates and
oversells fail closed. Corrections use explicit reversing or adjustment entries.

Broker holdings, positions, and cash remain authoritative external snapshots. Local
reconciliation reports differences without overwriting them. Corporate actions are not
silently inferred: until a reviewed corporate-action processor is implemented, dividends use
explicit journal entries and quantity/cost-basis changes require documented manual adjustments.

## Risk engine and kill switch

The conservative tracked risk configuration covers mode, live authorization, authentication,
account/IP identity, universe, data freshness, market consistency, session, strategy, signal,
duplicate intent, price/quantity, exposure, cash, loss, order-frequency, edge,
reconciliation, incident, and kill-switch gates. Every decision stores its complete snapshot,
configuration hash, approved quantity, and visible reason codes.

```powershell
uv run pq kill-switch-on --reason "operator safety stop"
uv run pq kill-switch-status
uv run pq kill-switch-reset --reconciled --confirm
```

The kill switch survives process restart. Reset requires both a healthy reconciliation and
explicit human confirmation. Automated circuit breakers use the same persistent mechanism.

## Order management and paper execution

Migration `0004` persists risk-linked limit orders, explicit state transitions, broker fills,
and reconciliation incidents. Submission uses deterministic client IDs and is attempted once:
an ambiguous timeout moves the order to `RECONCILIATION_REQUIRED`, blocks new orders for the
instrument, and requires broker-order reconciliation before any resubmission. Partial fills,
cancellation, at most two modifications, restart recovery, and fill deduplication are covered
by deterministic golden scenarios.

`PaperBroker` fills eligible limit orders only on a later bar whose range touches the limit.
Its fill quantity is capped by configured bar-volume participation, making the execution
assumption explicit and reproducible.

## Event-driven backtesting

The backtester consumes timestamp-ordered market events through an injected simulated clock.
Strategy-neutral signal callbacks run only after the current bar is valued, and resulting
orders cannot fill before a later bar. Next-open and conservative limit-touch fills are
available with fixed, spread, volatility, participation, symbol, and time-of-day slippage
models. Short sales fail closed.

Each deterministic run reports portfolio and trade metrics plus base, 1.5×, and 2× cost
cases. The immutable artefact writer records configuration, data and strategy manifests,
environment provenance, fills, trades, positions, equity, costs, metrics, warnings, logs, and
an equity chart with checksums. See [`config/backtest.example.yaml`](config/backtest.example.yaml)
for the initial execution assumptions.

## Baseline strategy

`baseline_momentum_v1` is a deliberately simple long-only engineering baseline. It emits
target-position signals—not broker orders—using completed-bar fast/slow trends, recent
volatility, average traded value, and an externally supplied market-regime flag. Entry also
requires configured expected edge to exceed estimated costs plus an uncertainty buffer; exits
cover trend reversal, time stop, and risk stop.

The same strategy contract is adapted to backtest and paper market events without importing a
broker in strategy code. Parameters are validated and fingerprinted in
[`config/strategies/baseline_momentum_v1.yaml`](config/strategies/baseline_momentum_v1.yaml).
Its hypothesis, assumptions, validation requirements, failure regimes, release status, and
changelog are recorded in the
[`strategy card`](docs/strategy_cards/baseline_momentum_v1_1.0.0.md). A deterministic
buy-and-hold comparator is included. This baseline validates engineering behavior and makes no
profitability claim.

## Branch flow

Changes begin on a focused work-package branch and flow through `dev`, `qa`, and `main`.
The `main` branch is reserved for approved stable releases.

## Security and local data

Never commit broker credentials, access tokens, passwords, TOTP seeds, demat PINs, account
details, databases, market datasets, logs, reports, or backups. Local runtime artefacts are
excluded by `.gitignore`.

## Current limitations

- Live WebSocket market-data collection is not yet implemented.
- Strategy research has not yet earned evidence beyond engineering validation; no strategy is
  approved for live trading.
- Sandbox support does not imply approval for live trading. Production authentication and
  production order routing remain unavailable.
- Production broker routing, live market-data collection, and trading orchestration remain
  unavailable; the OMS currently targets mock and paper brokers only.
- Python support is intentionally restricted to the installed Python 3.11 series.

# Personal Quant Trading System

A local-first, safety-focused personal quant trading system built from the engineering
contract in [`PERSONAL_QUANT_SYSTEM_BLUEPRINT.md`](PERSONAL_QUANT_SYSTEM_BLUEPRINT.md).

The project has completed **WP-00 through WP-13**, the **WP-14 runtime foundation**, the
**WP-15 local monitoring and control foundation**, the **WP-16 shadow-mode foundation**, and the
**WP-17 feature-gated Zerodha production-adapter foundation**,
including portfolio accounting, a
fail-closed pre-trade risk engine, a persistent paper-trading OMS, a deterministic
event-driven backtester, a broker-independent baseline strategy, and replayable live-data
collection. It has no production broker adapter and cannot place, modify, or cancel real-money
orders. The WP-14 non-counting readiness rehearsal passed on 2026-07-30, followed by the first
accepted live dry session. Revised hybrid acceptance remains pending four live dry sessions and
30 historical paper replay dates.

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
uv run pq historical-download --production --instrument NSE:INFY `
  --start 2026-01-01 --end 2026-07-28 --interval day `
  --snapshot data/reference/instruments/provider=zerodha/date=YYYY-MM-DD
```

The downloader is limited to two historical requests per second. Exact reruns reuse the
existing manifest without another broker call. Raw and curated data are separate immutable
Parquet layers; malformed batches are quarantined, and gaps are reported without forward
filling. Runtime market datasets remain excluded from Git.

## Historical paper sessions

WP-14 hybrid validation replays one complete historical market date through the same strategy,
risk, `PaperBroker`, OMS, delivery-cost, accounting, reconciliation, snapshot, and reporting
components used by the live paper runtime. It does not connect to a WebSocket or claim to validate
authentication, current-data freshness, networking, wall-clock scheduling, or reconnect behavior.

Download a gap-free minute-candle date, place its immutable manifest path and market date in
[`config/historical_paper.example.yaml`](config/historical_paper.example.yaml), then run:

```powershell
uv run pq historical-paper-session --config config/historical_paper.example.yaml `
  --confirm "START HISTORICAL PAPER REPLAY"

uv run pq hybrid-evidence-status --operational-path state/trading.sqlite `
  --replay-path state/replay/trading.sqlite
```

Replay state is isolated under `state/replay`. Only one clean, checksum-verified replay may count
per historical market date. Revised WP-14 acceptance requires 30 replay dates plus five clean live
dry sessions; replay never substitutes for the live-only checks.

For multiple completed dates, use the fail-closed
[`WP-14 controlled historical replay batch`](docs/runbooks/WP14_HISTORICAL_BATCH.md). It downloads,
validates, replays, audits, and backs up each date sequentially, retaining local transcripts and
stopping at the first anomaly.

## Quantitative research lab

QR-00 defines a research-only governance layer that runs in parallel with WP-14 without changing
its strategy, wallet, evidence, or reports. Research paths, point-in-time data requirements,
disjoint evaluation windows, mandatory cost stresses, and disabled production routing are
machine-validated. See the
[`quantitative research governance contract`](docs/QUANT_RESEARCH_GOVERNANCE.md).

```powershell
uv run pq research-governance-check
uv run pq research-manifest-check --manifest config/research/experiment.example.yaml
uv run pq research-universe-build
```

QR-01 derives immutable exact-date NSE membership from the locally retained instrument snapshots;
it never backfills missing dates. See the
[`point-in-time universe contract`](docs/POINT_IN_TIME_UNIVERSE.md).

QR-02 adds checksummed experiment/result storage, purged and embargoed walk-forward folds,
validation-only selection, and a one-use final holdout. See the
[`research experiment contract`](docs/RESEARCH_EXPERIMENTS.md).

QR-03 establishes cost-aware cash and equal-weight controls before any complex challenger is
considered. Validate them with `uv run pq research-benchmarks-check`; see the
[`research benchmark contract`](docs/RESEARCH_BENCHMARKS.md).

QR-04 adds a lagged, point-in-time cross-sectional momentum challenger with inverse-volatility
sizing and turnover buffers. Validate its contract with `uv run pq research-momentum-check`; see
[`CROSS_SECTIONAL_MOMENTUM.md`](docs/CROSS_SECTIONAL_MOMENTUM.md).

QR-05 adds a liquidity-filtered, regime-aware cross-sectional mean-reversion challenger. Validate
its contract with `uv run pq research-mean-reversion-check`; see
[`REGIME_MEAN_REVERSION.md`](docs/REGIME_MEAN_REVERSION.md).

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

## Live data and replay

The WebSocket collector is transport-injected and subscribes only to its approved instrument
map in an explicit `ltp`, `quote`, or `full` mode. Feed health fails closed on disconnect,
missing heartbeat, stale quotes, incomplete subscriptions, malformed data, and reconnect. A
reconnected socket remains in `AWAITING_FRESH_DATA` until every approved instrument has a fresh
valid quote from the new connection generation.

Accepted ticks and lifecycle/order-update events are recorded to immutable compressed Parquet
with a row-count and SHA-256 manifest. Exact duplicates are no-ops; conflicting, unknown,
crossed, stale, future, and out-of-order ticks become recorded data-quality violations. Replay
verifies the checksum and drives an injected simulated clock at any positive speed while
preserving the identical event stream. Initial assumptions are in
[`config/live_data.example.yaml`](config/live_data.example.yaml), with recovery steps in the
[`WebSocket reconnect runbook`](docs/runbooks/WEBSOCKET_RECONNECT.md).

## Paper runtime foundation

The paper runtime composes calendar scheduling, feed health, the baseline strategy, persistent
risk decisions, OMS, paper execution, and portfolio accounting behind explicit lifecycle
states. Pre-flight holds a PID/session lock and checks provenance, disk, database integrity,
authentication/account facts, instrument and calendar readiness, fresh feed state, kill switch,
reconciliation, and open-order recovery before enabling evaluation.

Every session persists lifecycle evidence and state snapshots. Graceful shutdown stops new
work, handles open orders, reconciles, writes an immutable daily capital/P&L/order report, and
releases the lock. Unfinished sessions are marked interrupted on recovery. Formal evidence is
locked until ten clean dry sessions exist, and only clean reconciled sessions without an active
kill switch count. See [`config/paper_runtime.example.yaml`](config/paper_runtime.example.yaml)
and the [`paper session evidence protocol`](docs/PAPER_SESSION_PROTOCOL.md).

Operational evidence can be inspected without mutation with:

```powershell
uv run pq paper-evidence-status --path F:\Quant_Trader\state\trading.sqlite
```

The [`WP-14 operational validation runbook`](docs/runbooks/WP14_OPERATIONAL_VALIDATION.md)
records the passed 2026-07-30 operator readiness rehearsal and governs evidence collection. The
current-data runner routes all intents exclusively to `PaperBroker`. Tests, rehearsal artifacts,
replay, accelerated clocks, and manually inserted rows never count as operational evidence.

## Local monitoring and controls

WP-15 provides a localhost-only FastAPI service and a separate Streamlit dashboard for system,
capital/P&L, orders/fills, strategy, risk, costs, reconciliation, and control-audit views. The
dashboard reads through HTTP and never opens the trading database. State-changing controls
require a 32-character-or-longer bearer token, an actor, a reason, and an exact confirmation;
every denied, failed, or successful attempt is written to the control audit. There is no live
trading enable control.

In separate PowerShell terminals, set the same database path for the API and runtime, then run:

```powershell
$env:PQ_DATABASE_PATH = "F:\Quant_Trader\state\trading.sqlite"
$env:PQ_DASHBOARD_CONTROL_TOKEN = "replace-with-a-random-secret-of-at-least-32-characters"
uv run pq-api
uv run streamlit run src/personal_quant/dashboard.py
```

Open `http://127.0.0.1:8501`. The API binds to `127.0.0.1:8765` by default. Do not put the
control token in Git; enter it only for a control operation. Dashboard/API failure is isolated
from the paper engine. WP-14 operational acceptance remains pending until its required real
session evidence is complete.

## Shadow mode foundation

WP-16 adds a deliberately read-only broker adapter, immutable broker snapshots, intended-order
comparison, and checksum-backed difference reports. Its adapter has no `place_order`,
`modify_order`, or `cancel_order` method, and reports explicitly record that transmission is
unavailable. Initial assumptions are in
[`config/shadow.example.yaml`](config/shadow.example.yaml).

Operational shadow execution remains fail-closed until the revised WP-14 hybrid gate is met and
reviewed: 30 historical replay dates plus five clean live dry sessions. That acceptance remains
**pending**; implementing the shadow foundation does not waive or fabricate its evidence.

## Feature-gated production adapter

WP-17 adds human-mediated production authentication, expected-account and public-IP pre-flight,
restricted LIMIT/CNC order mapping, asynchronous order-update mapping, and broker/local
reconciliation. The checked-in live configuration keeps routing disabled, and every broker write
fails closed until the feature flag plus paper, shadow, identity, capability, and IP gates all
pass. See the [`production adapter safety contract`](docs/PRODUCTION_ADAPTER.md).

WP-14 operational acceptance and production approval remain **pending**. No live-start command or
automatic production activation exists.

## Branch flow

Changes begin on a focused work-package branch and flow through `dev`, `qa`, and `main`.
The `main` branch is reserved for approved stable releases.

## Security and local data

Never commit broker credentials, access tokens, passwords, TOTP seeds, demat PINs, account
details, databases, market datasets, logs, reports, or backups. Local runtime artefacts are
excluded by `.gitignore`.

## Current limitations

- The production-authenticated WebSocket runner is restricted to current-data input and
  `PaperBroker`; its readiness rehearsal and Dry Session 1 passed, while WP-14 evidence collection
  remains pending.
- Strategy research has not yet earned evidence beyond engineering validation; no strategy is
  approved for live trading.
- Revised WP-14 hybrid evidence has reached 1/5 live dry sessions and 1/30 historical replay
  sessions; the two evidence sources remain isolated and independently auditable.
- Sandbox support does not imply approval for live trading. Production authentication and
  production order routing remain unavailable.
- Production broker routing and live trading orchestration remain feature-gated and unavailable;
  the operational session runner deliberately targets `PaperBroker` only.
- Python support is intentionally restricted to the installed Python 3.11 series.

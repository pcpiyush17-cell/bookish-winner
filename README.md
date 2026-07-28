# Personal Quant Trading System

A local-first, safety-focused personal quant trading system built from the engineering
contract in [`PERSONAL_QUANT_SYSTEM_BLUEPRINT.md`](PERSONAL_QUANT_SYSTEM_BLUEPRINT.md).

The project has completed **WP-00 through WP-05**, including immutable instrument-master
snapshots, a versioned NSE market calendar, and historical candle ingestion. It has no
production broker adapter and cannot place, modify, or cancel real-money orders.

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

## Branch flow

Changes begin on a focused work-package branch and flow through `dev`, `qa`, and `main`.
The `main` branch is reserved for approved stable releases.

## Security and local data

Never commit broker credentials, access tokens, passwords, TOTP seeds, demat PINs, account
details, databases, market datasets, logs, reports, or backups. Local runtime artefacts are
excluded by `.gitignore`.

## Current limitations

- Live WebSocket market-data collection is not yet implemented.
- Sandbox support does not imply approval for live trading. Production authentication and
  production order routing remain unavailable.
- Broker, data, storage, risk, accounting, and trading functionality belong to later work
  packages.
- Python support is intentionally restricted to the installed Python 3.11 series.

# Personal Quant Trading System

A local-first, safety-focused personal quant trading system built from the engineering
contract in [`PERSONAL_QUANT_SYSTEM_BLUEPRINT.md`](PERSONAL_QUANT_SYSTEM_BLUEPRINT.md).

The project has completed **WP-00 through WP-02**, including the bootstrap, shared-domain
foundations, and SQLite storage. It contains no broker integration and cannot place, modify,
or cancel orders.

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

## Branch flow

Changes begin on a focused work-package branch and flow through `dev`, `qa`, and `main`.
The `main` branch is reserved for approved stable releases.

## Security and local data

Never commit broker credentials, access tokens, passwords, TOTP seeds, demat PINs, account
details, databases, market datasets, logs, reports, or backups. Local runtime artefacts are
excluded by `.gitignore`.

## Current limitations

- Only the bootstrap CLI, shared domain contracts, configuration, and foundational SQLite
  storage are implemented.
- Broker, data, storage, risk, accounting, and trading functionality belong to later work
  packages.
- Python support is intentionally restricted to the installed Python 3.11 series.

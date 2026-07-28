# Personal Quant Trading System

A local-first, safety-focused personal quant trading system built from the engineering
contract in [`PERSONAL_QUANT_SYSTEM_BLUEPRINT.md`](PERSONAL_QUANT_SYSTEM_BLUEPRINT.md).

The project is currently at **WP-00: Project bootstrap**. It contains no broker integration
and cannot place, modify, or cancel orders.

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

## Branch flow

Changes begin on a focused work-package branch and flow through `dev`, `qa`, and `main`.
The `main` branch is reserved for approved stable releases.

## Security and local data

Never commit broker credentials, access tokens, passwords, TOTP seeds, demat PINs, account
details, databases, market datasets, logs, reports, or backups. Local runtime artefacts are
excluded by `.gitignore`.

## Current limitations

- Only the bootstrap CLI and environment diagnostics are implemented.
- Broker, data, storage, risk, accounting, and trading functionality belong to later work
  packages.
- Python support is intentionally restricted to the installed Python 3.11 series.

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from personal_quant.broker.auth import TokenStore
from personal_quant.cli import app
from personal_quant.instruments import InstrumentSnapshotStore

from .test_instruments import row
from .test_sandbox_adapter import FakeKiteClient

runner = CliRunner()


class InstrumentClient(FakeKiteClient):
    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        return [row()]


def test_reference_validation_and_calendar_commands(tmp_path: Path) -> None:
    root = tmp_path / "instruments"
    InstrumentSnapshotStore(root).save(
        rows=[row()], snapshot_date=date(2026, 7, 28), downloaded_at=datetime.now(UTC)
    )
    directory = root / "provider=zerodha" / "date=2026-07-28"
    result = runner.invoke(app, ["instruments-validate", "--directory", str(directory)])
    assert result.exit_code == 0
    assert "[PASS]" in result.stdout
    result = runner.invoke(app, ["calendar-check", "--date", "2026-10-02"])
    assert result.exit_code == 0
    assert "CLOSED" in result.stdout
    result = runner.invoke(app, ["calendar-check", "--date", "not-a-date"])
    assert result.exit_code == 1
    assert "calendar_date_invalid" in result.stderr


def test_instrument_download_uses_stored_sandbox_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token.json"
    TokenStore(token_path).save(
        access_token="fixture-access", user_id="SANDBOX01", authenticated_at="2026-07-28"
    )
    client = InstrumentClient()
    monkeypatch.setenv("KITE_SANDBOX_API_KEY", "fixture-key")
    monkeypatch.setattr("personal_quant.cli.create_sandbox_client", lambda api_key: client)
    result = runner.invoke(
        app,
        [
            "instruments-download",
            "--root",
            str(tmp_path / "snapshots"),
            "--token-path",
            str(token_path),
        ],
    )
    assert result.exit_code == 0
    assert "1 NSE equities" in result.stdout
    assert "fixture-access" not in result.stdout


def test_instrument_download_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KITE_SANDBOX_API_KEY", raising=False)
    result = runner.invoke(app, ["instruments-download"])
    assert result.exit_code == 1
    assert "sandbox_api_key_missing" in result.stderr


def test_instrument_download_uses_validated_production_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "production-token.json"
    TokenStore(token_path).save(
        access_token="production-access",
        user_id="ALU209",
        authenticated_at="2026-07-29T10:00:00+05:30",
    )
    client = InstrumentClient()
    monkeypatch.setenv("KITE_API_KEY", "production-key")
    monkeypatch.setenv("KITE_EXPECTED_USER_ID", "ALU209")
    monkeypatch.setattr("personal_quant.cli.create_production_client", lambda api_key: client)

    result = runner.invoke(
        app,
        [
            "instruments-download",
            "--production",
            "--root",
            str(tmp_path / "snapshots"),
            "--token-path",
            str(token_path),
        ],
    )

    assert result.exit_code == 0
    assert "1 NSE equities" in result.stdout
    assert "order routing remains disabled" in result.stdout
    assert "production-access" not in result.stdout


def test_production_instrument_download_rejects_token_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "production-token.json"
    TokenStore(token_path).save(
        access_token="production-access",
        user_id="OTHER01",
        authenticated_at="2026-07-29T10:00:00+05:30",
    )
    monkeypatch.setenv("KITE_API_KEY", "production-key")
    monkeypatch.setenv("KITE_EXPECTED_USER_ID", "ALU209")

    result = runner.invoke(
        app,
        ["instruments-download", "--production", "--token-path", str(token_path)],
    )

    assert result.exit_code == 1
    assert "production_token_identity_mismatch" in result.stderr
    assert "production-access" not in result.stderr


def test_historical_download_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KITE_SANDBOX_API_KEY", raising=False)
    result = runner.invoke(
        app,
        [
            "historical-download",
            "--instrument",
            "NSE:INFY",
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-02",
            "--snapshot",
            "missing",
        ],
    )
    assert result.exit_code == 1
    assert "sandbox_api_key_missing" in result.stderr

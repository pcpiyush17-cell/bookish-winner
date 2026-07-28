from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app

from .test_sandbox_adapter import FakeKiteClient

runner = CliRunner()


def configure_sandbox(monkeypatch: pytest.MonkeyPatch) -> FakeKiteClient:
    client = FakeKiteClient()
    monkeypatch.setenv("KITE_SANDBOX_API_KEY", "fixture-key")
    monkeypatch.setattr("personal_quant.cli.create_sandbox_client", lambda api_key: client)
    return client


def test_kite_login_prints_sandbox_url_without_automating_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_sandbox(monkeypatch)

    result = runner.invoke(app, ["kite-login"])

    assert result.exit_code == 0
    assert "sandbox.kite.trade" in result.stdout
    assert "two-factor authentication in your browser" in result.stdout
    assert "--exchange" in result.stdout


def test_kite_login_exchanges_hidden_token_and_redacts_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_sandbox(monkeypatch)
    monkeypatch.setenv("KITE_SANDBOX_API_SECRET", "fixture-secret")
    monkeypatch.setenv("KITE_SANDBOX_EXPECTED_USER_ID", "SANDBOX01")
    token_path = tmp_path / "state" / "token.json"

    result = runner.invoke(
        app,
        ["kite-login", "--exchange", "--token-path", str(token_path)],
        input="short-lived-request-token\n",
    )

    assert result.exit_code == 0
    assert "Sandbox authenticated for user: SANDBOX01" in result.stdout
    assert token_path.is_file()
    assert "short-lived-request-token" not in result.stdout
    assert "fixture-secret" not in result.stdout


def test_kite_login_requires_environment_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KITE_SANDBOX_API_KEY", raising=False)
    result = runner.invoke(app, ["kite-login"])
    assert result.exit_code == 1
    assert "sandbox_api_key_missing" in result.stderr

    configure_sandbox(monkeypatch)
    monkeypatch.delenv("KITE_SANDBOX_API_SECRET", raising=False)
    monkeypatch.delenv("KITE_SANDBOX_EXPECTED_USER_ID", raising=False)
    result = runner.invoke(app, ["kite-login", "--exchange"])
    assert result.exit_code == 1
    assert "sandbox_auth_config_missing" in result.stderr

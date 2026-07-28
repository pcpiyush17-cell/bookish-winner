from typer.testing import CliRunner

from personal_quant import __version__
from personal_quant.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_doctor_reports_ready_environment() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "[PASS] Python: 3.11.9" in result.stdout
    assert "Environment is ready for local development." in result.stdout

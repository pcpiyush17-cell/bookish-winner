from pathlib import Path

from typer.testing import CliRunner

from personal_quant.cli import app

runner = CliRunner()


def test_kill_switch_cli_lifecycle(tmp_path: Path) -> None:
    database = tmp_path / "risk.sqlite"
    initialized = runner.invoke(app, ["init-db", "--path", str(database)])
    assert initialized.exit_code == 0
    activated = runner.invoke(
        app, ["kill-switch-on", "--reason", "operator test", "--path", str(database)]
    )
    assert activated.exit_code == 0
    assert "activated" in activated.stdout
    status = runner.invoke(app, ["kill-switch-status", "--path", str(database)])
    assert status.stdout.strip() == "ACTIVE"
    denied = runner.invoke(app, ["kill-switch-reset", "--path", str(database)])
    assert denied.exit_code == 1
    assert "kill_switch_reset_denied" in denied.stderr
    reset = runner.invoke(
        app,
        ["kill-switch-reset", "--reconciled", "--confirm", "--path", str(database)],
    )
    assert reset.exit_code == 0
    assert (
        runner.invoke(app, ["kill-switch-status", "--path", str(database)]).stdout.strip()
        == "INACTIVE"
    )

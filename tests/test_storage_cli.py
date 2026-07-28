from pathlib import Path

from typer.testing import CliRunner

from personal_quant.cli import app

runner = CliRunner()


def test_storage_cli_lifecycle(tmp_path: Path) -> None:
    database = tmp_path / "state" / "trading.sqlite"
    backup = tmp_path / "backups" / "trading.sqlite"

    initialized = runner.invoke(app, ["init-db", "--path", str(database)])
    checked = runner.invoke(app, ["db-check", "--path", str(database)])
    backed_up = runner.invoke(
        app,
        ["backup", "--path", str(database), "--destination", str(backup)],
    )

    assert initialized.exit_code == 0
    assert "Migrations applied: 0001, 0002, 0003" in initialized.stdout
    assert checked.exit_code == 0
    assert "[PASS] Database integrity" in checked.stdout
    assert backed_up.exit_code == 0
    assert "Backup created and verified" in backed_up.stdout
    assert backup.is_file()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "trading.sqlite"
    runner.invoke(app, ["init-db", "--path", str(database)])

    result = runner.invoke(app, ["init-db", "--path", str(database)])

    assert result.exit_code == 0
    assert "Migrations applied: none" in result.stdout


def test_storage_cli_reports_structured_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["db-check", "--path", str(tmp_path / "missing.sqlite")])

    assert result.exit_code == 1
    assert "Storage error [database_not_found]" in result.stderr

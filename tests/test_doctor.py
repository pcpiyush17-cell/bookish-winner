from pathlib import Path

from personal_quant.doctor import run_checks


def test_checks_pass_for_safe_writable_directory(tmp_path: Path) -> None:
    checks = run_checks(tmp_path)

    assert all(check.passed for check in checks)


def test_secret_file_fails_safety_check(tmp_path: Path) -> None:
    (tmp_path / ".env").touch()

    checks = run_checks(tmp_path)
    safety = next(check for check in checks if check.name == "Secret-file safety")

    assert not safety.passed
    assert safety.detail == "found: .env"

"""Read-only local environment diagnostics."""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

REQUIRED_PYTHON = (3, 11, 9)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The result of one environment check."""

    name: str
    passed: bool
    detail: str


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _python_check() -> CheckResult:
    actual = sys.version_info[:3]
    passed = actual == REQUIRED_PYTHON
    detail = f"{actual[0]}.{actual[1]}.{actual[2]} (required: 3.11.9)"
    return CheckResult("Python", passed, detail)


def _project_directory_check(root: Path) -> CheckResult:
    exists = root.is_dir()
    writable = os.access(root, os.W_OK) if exists else False
    return CheckResult("Project directory", exists and writable, f"{root} (writable={writable})")


def _sqlite_check() -> CheckResult:
    version = sqlite3.sqlite_version
    return CheckResult("SQLite", True, version)


def _safety_check(root: Path) -> CheckResult:
    forbidden = (root / ".env", root / "access_token")
    found = [path.name for path in forbidden if path.exists()]
    detail = "no common secret files found" if not found else f"found: {', '.join(found)}"
    return CheckResult("Secret-file safety", not found, detail)


def run_checks(root: Path | None = None) -> tuple[CheckResult, ...]:
    """Run deterministic, non-networked environment checks."""
    project_root = root if root is not None else _project_root()
    return (
        _python_check(),
        _project_directory_check(project_root),
        _sqlite_check(),
        _safety_check(project_root),
    )

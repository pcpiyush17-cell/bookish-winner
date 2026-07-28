"""Operator-facing storage maintenance services."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from personal_quant.clocks import Clock


def timestamped_backup_path(database: Path, backup_root: Path, clock: Clock) -> Path:
    """Build a deterministic backup name using an injected clock."""
    timestamp = clock.now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return backup_root / f"{database.stem}-{timestamp}.sqlite"

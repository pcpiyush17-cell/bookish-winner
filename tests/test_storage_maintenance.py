from datetime import UTC, datetime
from pathlib import Path

from personal_quant.clocks import SimulatedClock
from personal_quant.storage.maintenance import timestamped_backup_path


def test_timestamped_backup_path_uses_injected_clock() -> None:
    clock = SimulatedClock(datetime(2026, 7, 28, 10, 5, 6, tzinfo=UTC))

    result = timestamped_backup_path(Path("state/trading.sqlite"), Path("backups"), clock)

    assert result == Path("backups/trading-20260728T100506Z.sqlite")

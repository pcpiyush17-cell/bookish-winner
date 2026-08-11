import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.historical_paper import (
    HistoricalPaperConfig,
    HistoricalPaperError,
    load_historical_source,
    run_historical_paper_session,
)
from personal_quant.paper_evidence import audit_hybrid_evidence
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner

ROOT = Path(__file__).parents[1]
DAY = date(2026, 7, 29)
CLI = CliRunner()


def replay_config(tmp_path: Path) -> HistoricalPaperConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    curated = tmp_path / "interval=15minute" / "curated.parquet"
    curated.parent.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 7, 29, 3, 45, tzinfo=UTC)
    rows = []
    for index in range(25):
        price = Decimal("100") + Decimal(index % 3) / 10
        rows.append(
            {
                "instrument_key": "NSE:INFY",
                "instrument_token": 408065,
                "interval": "15minute",
                "timestamp": start + timedelta(minutes=15 * index),
                "open": str(price),
                "high": str(price + Decimal("0.20")),
                "low": str(price - Decimal("0.20")),
                "close": str(price + Decimal("0.10")),
                "volume": 1000,
                "oi": None,
            }
        )
    pq.write_table(pa.Table.from_pylist(rows), curated)
    checksum = hashlib.sha256(curated.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "status": "complete",
                "invalid_rows": 0,
                "curated_rows": len(rows),
                "gaps": [],
                "curated_path": str(curated),
                "curated_checksum_sha256": checksum,
                "request": {
                    "instrument_key": "NSE:INFY",
                    "interval": "15minute",
                    "start": "2026-07-29T09:15:00+05:30",
                    "end": "2026-07-29T15:30:00+05:30",
                },
            }
        ),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "mode: paper",
                "evidence_kind: dry",
                "minimum_free_disk_mb: 1",
                'opening_cash_inr: "10000.00"',
                'tick_size_inr: "0.05"',
                'expected_gross_edge_inr: "50.00"',
                'expected_costs_inr: "10.00"',
                f"cost_config: {ROOT / 'config/costs/zerodha_nse_delivery_2026-07-28.yaml'}",
                'spread_bps: "1.00"',
                'slippage_bps: "1.00"',
                'impact_bps: "0.00"',
                'maximum_price_deviation_pct: "2.00"',
                'stop_loss_pct: "5.00"',
                "cancel_open_orders_on_shutdown: true",
                f"report_root: {tmp_path / 'reports'}",
                f"lock_path: {tmp_path / 'replay.lock'}",
            )
        ),
        encoding="utf-8",
    )
    return HistoricalPaperConfig(
        schema_version=1,
        database_path=tmp_path / "replay.sqlite",
        runtime_config=runtime,
        risk_config=ROOT / "config/risk/conservative_10k.yaml",
        strategy_config=ROOT / "config/strategies/baseline_momentum_v1.yaml",
        calendar_config=ROOT / "config/calendars/nse_equity_2026.yaml",
        historical_manifest=manifest,
        market_date=DAY,
        instrument=InstrumentKey("NSE:INFY"),
    )


def test_historical_paper_session_is_isolated_deterministic_evidence(tmp_path: Path) -> None:
    config = replay_config(tmp_path)

    result = run_historical_paper_session(config)

    assert result.market_date == DAY
    assert result.bars == 25
    assert result.report_path.is_file()
    connection = Database(config.database_path).connect(read_only=True)
    try:
        source = connection.execute(
            "SELECT evidence_source FROM paper_runtime_sessions WHERE session_id=?",
            (result.session_id,),
        ).fetchone()[0]
        replay = connection.execute(
            "SELECT market_date, deterministic FROM historical_paper_sessions"
        ).fetchone()
    finally:
        connection.close()
    assert source == "replay"
    assert tuple(replay) == (DAY.isoformat(), 1)

    with pytest.raises(HistoricalPaperError) as error:
        run_historical_paper_session(config)
    assert error.value.code == "historical_paper_date_duplicate"


def test_historical_source_checksum_fails_closed(tmp_path: Path) -> None:
    config = replay_config(tmp_path)
    config.historical_manifest.write_text(
        config.historical_manifest.read_text(encoding="utf-8").replace(
            '"curated_checksum_sha256": "', '"curated_checksum_sha256": "0'
        ),
        encoding="utf-8",
    )

    with pytest.raises(HistoricalPaperError) as error:
        load_historical_source(config)
    assert error.value.code == "historical_source_checksum"


def test_hybrid_audit_keeps_live_and_replay_counts_separate(tmp_path: Path) -> None:
    config = replay_config(tmp_path / "replay")
    run_historical_paper_session(config)
    operational = Database(tmp_path / "operational.sqlite")
    MigrationRunner(operational).apply_all()

    audit = audit_hybrid_evidence(operational, Database(config.database_path))

    assert audit.live_dry_sessions == 0
    assert audit.replay_sessions == 1
    assert not audit.acceptance_met
    assert "5 live dry sessions remain" in audit.blockers

    result = CLI.invoke(
        app,
        [
            "hybrid-evidence-status",
            "--operational-path",
            str(operational.path),
            "--replay-path",
            str(config.database_path),
        ],
    )
    assert result.exit_code == 0
    assert "Audited live dry sessions: 0/5" in result.stdout
    assert "Audited historical replay sessions: 1/30" in result.stdout

    Path(
        json.loads(config.historical_manifest.read_text(encoding="utf-8"))["curated_path"]
    ).unlink()
    invalid = audit_hybrid_evidence(operational, Database(config.database_path))
    assert invalid.replay_sessions == 0
    assert "Historical replay evidence contains unresolved integrity issues" in invalid.blockers


def test_historical_paper_cli_requires_exact_confirmation(tmp_path: Path) -> None:
    result = CLI.invoke(
        app,
        ["historical-paper-session", "--config", str(tmp_path / "missing.yaml")],
    )

    assert result.exit_code == 1
    assert "historical_paper_confirmation_invalid" in result.stderr


def test_historical_paper_cli_runs_confirmed_replay(tmp_path: Path) -> None:
    config = replay_config(tmp_path)
    config_path = tmp_path / "historical-paper.yaml"
    config_path.write_text(
        "\n".join(
            (
                "schema_version: 1",
                f"database_path: {config.database_path}",
                f"runtime_config: {config.runtime_config}",
                f"risk_config: {config.risk_config}",
                f"strategy_config: {config.strategy_config}",
                f"calendar_config: {config.calendar_config}",
                f"historical_manifest: {config.historical_manifest}",
                f"market_date: {DAY.isoformat()}",
                "instrument: NSE:INFY",
            )
        ),
        encoding="utf-8",
    )

    result = CLI.invoke(
        app,
        [
            "historical-paper-session",
            "--config",
            str(config_path),
            "--confirm",
            "START HISTORICAL PAPER REPLAY",
        ],
    )

    assert result.exit_code == 0
    assert "Bars replayed: 25" in result.stdout
    assert "This replay is not live-market operational evidence" in result.stdout


def test_historical_paper_config_load_fails_closed(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: wrong\n", encoding="utf-8")

    with pytest.raises(HistoricalPaperError) as error:
        HistoricalPaperConfig.load(invalid)
    assert error.value.code == "historical_paper_config_invalid"


def test_historical_paper_rejects_live_database_path(tmp_path: Path) -> None:
    config = replay_config(tmp_path).model_copy(
        update={"database_path": Path("state/trading.sqlite")}
    )

    with pytest.raises(HistoricalPaperError) as error:
        run_historical_paper_session(config)
    assert error.value.code == "historical_paper_database_unsafe"

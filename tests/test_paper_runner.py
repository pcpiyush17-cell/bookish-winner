import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from typer.testing import CliRunner

import personal_quant.paper_runner as paper_runner
from personal_quant.broker.auth import TokenStore
from personal_quant.cli import app
from personal_quant.domain.identifiers import InstrumentKey, InstrumentToken
from personal_quant.domain.money import Money
from personal_quant.instruments import InstrumentSnapshotStore
from personal_quant.live_data import LiveTick, WebSocketMode
from personal_quant.paper_runner import (
    BarAggregator,
    PaperRunnerError,
    RunnerConfig,
    bind_ticker_callbacks,
    build_operational_runner,
    parse_kite_tick,
)
from personal_quant.paper_runtime import RuntimeConfig, RuntimeState

NOW = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)


def live_tick(moment: datetime, price: str, volume: int) -> LiveTick:
    from uuid import uuid4

    return LiveTick(
        uuid4(),
        InstrumentToken(408065),
        InstrumentKey("NSE:INFY"),
        moment,
        moment,
        moment,
        moment,
        Money.from_value(price),
        Money.from_value("99"),
        Money.from_value("101"),
        1,
        volume,
        None,
        1,
    )


def test_runner_config_is_strict_and_keeps_dry_mode() -> None:
    config = RunnerConfig.load(Path("config/paper_runner.example.yaml"))
    assert config.evidence_kind.value == "dry"
    assert config.approved_instruments == (InstrumentKey("NSE:INFY"),)
    assert config.poll_interval_seconds.as_tuple().exponent == -1


def test_bar_aggregator_emits_only_completed_current_data_bars() -> None:
    aggregator = BarAggregator(60)
    assert aggregator.add(live_tick(NOW, "100", 1000)) is None
    assert aggregator.add(live_tick(NOW + timedelta(seconds=20), "102", 1010)) is None
    assert aggregator.add(live_tick(NOW + timedelta(seconds=40), "99", 1025)) is None

    bar = aggregator.add(live_tick(NOW + timedelta(seconds=61), "101", 1030))

    assert bar is not None
    assert (bar.open, bar.high, bar.low, bar.close) == tuple(
        Money.from_value(value) for value in ("100", "102", "99", "99")
    )
    assert bar.volume == 25


def test_kite_tick_parser_maps_quote_depth_and_rejects_incomplete_payload() -> None:
    raw = parse_kite_tick(
        {
            "instrument_token": 408065,
            "exchange_timestamp": NOW,
            "timestamp": NOW,
            "last_price": 1500.25,
            "last_quantity": 2,
            "volume_traded": 100,
            "depth": {
                "buy": [{"price": 1500.2}],
                "sell": [{"price": 1500.3}],
            },
        }
    )
    assert raw.bid == Money.from_value("1500.2")
    assert raw.ask == Money.from_value("1500.3")
    naive = parse_kite_tick(
        {
            "instrument_token": 408065,
            "timestamp": datetime(2026, 7, 29, 9, 30),
            "last_price": 1500,
        }
    )
    assert naive.exchange_timestamp.utcoffset() == timedelta(hours=5, minutes=30)
    with pytest.raises(Exception, match="incomplete"):
        parse_kite_tick({"instrument_token": 408065})
    with pytest.raises(Exception, match="incomplete"):
        parse_kite_tick({"instrument_token": 408065, "timestamp": "not-a-time", "last_price": 1})


class TickerStub:
    def connect(self, *, threaded: bool = False) -> None:
        return None

    def subscribe(self, instrument_tokens: list[int]) -> object:
        return None

    def set_mode(self, mode: str, instrument_tokens: list[int]) -> object:
        return None

    def close(self) -> None:
        return None


class CollectorStub:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on_connected(self) -> None:
        self.events.append("connected")

    def on_disconnected(self, code: int, reason: str) -> None:
        self.events.append(f"closed:{code}:{reason}")

    def on_order_update(self, payload: object) -> None:
        self.events.append("order_update")

    def on_ticks(self, ticks: object) -> tuple[object, ...]:
        self.events.append("ticks")
        return ()


def test_callback_bridge_connects_official_shapes_to_collector() -> None:
    ticker = TickerStub()
    collector = CollectorStub()
    bind_ticker_callbacks(ticker, collector)  # type: ignore[arg-type]
    ticker.on_connect(None, None)  # type: ignore[attr-defined]
    ticker.on_ticks(  # type: ignore[attr-defined]
        None,
        [
            {
                "instrument_token": 408065,
                "exchange_timestamp": NOW,
                "last_price": 100,
                "volume": 1,
            }
        ],
    )
    ticker.on_order_update(None, {})  # type: ignore[attr-defined]
    ticker.on_close(None, 1000, "done")  # type: ignore[attr-defined]
    assert collector.events == ["connected", "ticks", "order_update", "closed:1000:done"]


def test_runner_source_has_no_production_order_adapter_or_order_calls() -> None:
    source = Path("src/personal_quant/paper_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = [ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert all("ProductionKiteAdapter" not in value for value in imported)
    assert ".place_order(" not in source
    assert "PaperBroker(" in source


def test_cli_requires_exact_confirmation_before_assembly(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def forbidden(_config: RunnerConfig) -> Any:
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr("personal_quant.cli.build_operational_runner", forbidden)
    result = CliRunner().invoke(
        app,
        ["paper-session-start", "--config", "config/paper_runner.example.yaml"],
    )
    assert result.exit_code == 1
    assert "paper_confirmation_invalid" in result.stderr
    assert called is False


def test_invalid_runner_config_is_redacted(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("secret: should-not-be-echoed", encoding="utf-8")
    with pytest.raises(PaperRunnerError) as captured:
        RunnerConfig.load(path)
    assert captured.value.code == "paper_runner_config_invalid"
    assert "should-not-be-echoed" not in str(captured.value)


class ProfileClientStub:
    def set_access_token(self, _token: str) -> None:
        return None

    def profile(self) -> dict[str, object]:
        return {
            "user_id": "AB1234",
            "user_name": "Piyush Chandra",
            "broker": "ZERODHA",
            "exchanges": ["NSE"],
            "products": ["CNC"],
        }


class OperationalTickerStub(TickerStub):
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.closed = False

    def connect(self, *, threaded: bool = False) -> None:
        self.on_connect(self, None)  # type: ignore[attr-defined]
        self.on_ticks(  # type: ignore[attr-defined]
            self,
            [
                {
                    "instrument_token": 408065,
                    "exchange_timestamp": datetime.now(UTC),
                    "timestamp": datetime.now(UTC),
                    "last_price": 1500,
                    "last_quantity": 1,
                    "volume": 100,
                    "depth": {
                        "buy": [{"price": 1499.95}],
                        "sell": [{"price": 1500.05}],
                    },
                }
            ],
        )

    def close(self) -> None:
        self.closed = True


def instrument_row() -> dict[str, object]:
    return {
        "instrument_token": 408065,
        "exchange_token": "1594",
        "tradingsymbol": "INFY",
        "name": "INFOSYS",
        "expiry": "",
        "strike": "0",
        "tick_size": "0.05",
        "lot_size": 1,
        "instrument_type": "EQ",
        "segment": "NSE",
        "exchange": "NSE",
        "isin": "INE009A01021",
    }


def test_operational_assembly_runs_one_clean_temp_session_with_paper_broker_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    snapshot = InstrumentSnapshotStore(tmp_path / "instruments").save(
        rows=[instrument_row()],
        snapshot_date=now.astimezone(ZoneInfo("Asia/Kolkata")).date(),
        downloaded_at=now,
    )
    snapshot_directory = (
        tmp_path / "instruments" / "provider=zerodha" / f"date={snapshot.manifest.snapshot_date}"
    )
    runtime_raw = yaml.safe_load(
        Path("config/paper_runtime.example.yaml").read_text(encoding="utf-8")
    )
    runtime_raw["minimum_free_disk_mb"] = 1
    runtime_raw["report_root"] = str(tmp_path / "reports")
    runtime_raw["lock_path"] = str(tmp_path / "paper.lock")
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text(yaml.safe_dump(runtime_raw), encoding="utf-8")
    token_path = tmp_path / "token.json"
    TokenStore(token_path).save(
        access_token="read-only-current-data-token",
        user_id="AB1234",
        authenticated_at=now.isoformat(),
    )
    config = RunnerConfig(
        schema_version=1,
        evidence_kind="dry",  # type: ignore[arg-type]
        database_path=tmp_path / "trading.sqlite",
        runtime_config=runtime_path,
        risk_config=Path("config/risk/conservative_10k.yaml"),
        strategy_config=Path("config/strategies/baseline_momentum_v1.yaml"),
        calendar_config=Path("config/calendars/nse_equity_2026.yaml"),
        instrument_snapshot_directory=snapshot_directory,
        approved_instruments=(InstrumentKey("NSE:INFY"),),
        recording_root=tmp_path / "recordings",
        token_path=token_path,
        startup_timeout_seconds=5,
        poll_interval_seconds="0.01",  # type: ignore[arg-type]
        bar_interval_seconds=60,
    )
    monkeypatch.setenv("KITE_API_KEY", "fixture-key")
    monkeypatch.setenv("KITE_EXPECTED_USER_ID", "AB1234")
    monkeypatch.setattr(paper_runner, "create_production_client", lambda _key: ProfileClientStub())
    monkeypatch.setattr("kiteconnect.KiteTicker", OperationalTickerStub)

    runner = build_operational_runner(config)

    def finish(_seconds: float) -> None:
        runner.on_tick(live_tick(datetime.now(UTC) + timedelta(minutes=2), "1501", 120))
        runner.request_stop()

    runner.sleeper = finish
    report, recording = runner.run_until_stopped()

    assert runner.runtime.state is RuntimeState.STOPPED
    assert runner.runtime.broker.__class__.__name__ == "PaperBroker"
    assert runner.collector.config.mode is WebSocketMode.FULL
    assert report.clean_shutdown is True
    assert recording.manifest_path.exists()
    assert RuntimeConfig.load(runtime_path).mode == "paper"

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import personal_quant.dashboard as dashboard
from personal_quant.dashboard import DashboardClient, DashboardError
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner

NOW = datetime(2026, 7, 29, 5, tzinfo=UTC)


def test_dashboard_has_no_database_or_runtime_imports() -> None:
    source = Path("src/personal_quant/dashboard.py").read_text(encoding="utf-8")
    imports = [
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    prohibited = ("sqlite", "storage", "risk", "oms", "paper_runtime", "broker")
    assert all(not any(item in value for item in prohibited) for value in imports)


def test_dashboard_api_failure_does_not_change_running_engine_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "trading.sqlite")
    MigrationRunner(database).apply_all()
    run_id = uuid4()
    with database.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO runtime_sessions VALUES (?, ?, 'paper', ?, ?, NULL, 'running')",
            (str(uuid4()), str(run_id), "a" * 64, NOW.isoformat()),
        )
    with pytest.raises(DashboardError):
        DashboardClient("http://127.0.0.1:1", timeout_seconds=0.01).get("/health")
    connection = database.connect(read_only=True)
    try:
        state = connection.execute(
            "SELECT status FROM runtime_sessions WHERE run_id=?", (str(run_id),)
        ).fetchone()[0]
    finally:
        connection.close()
    assert state == "running"


def test_dashboard_rejects_non_loopback_api_url() -> None:
    with pytest.raises(DashboardError, match="loopback"):
        DashboardClient("https://example.com").get("/health")


class Widget:
    def __enter__(self) -> "Widget":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def metric(self, *_args: object, **_kwargs: object) -> None:
        return None


class StreamlitStub:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name == "columns":
            return lambda count: [Widget() for _ in range(count)]
        if name == "tabs":
            return lambda labels: [Widget() for _ in labels]
        if name == "text_input":
            return lambda *_args, **_kwargs: ""
        if name in {"button", "checkbox"}:
            return lambda *_args, **_kwargs: False
        if name == "error":
            return lambda value: self.errors.append(str(value))
        return lambda *_args, **_kwargs: None


class ClientStub:
    def get(self, path: str) -> dict[str, Any]:
        responses: dict[str, dict[str, Any]] = {
            "/metrics/system": {
                "mode": "paper",
                "latest_session": {"state": "running"},
                "kill_switch_active": False,
                "wp14_operational_acceptance": {
                    "status": "pending",
                    "dry_sessions": 3,
                    "formal_sessions": 0,
                },
            },
            "/metrics/capital": {
                "cash_inr": "100.00",
                "market_value_inr": "50.00",
                "net_liquidation_value_inr": "150.00",
                "trading_net_pnl_inr": "1.00",
                "positions": [],
            },
            "/metrics/orders": {"orders": [], "fills": []},
            "/metrics/risk": {"decisions": [], "incidents": []},
            "/metrics/costs": {
                "components": [{"component": "tax", "amount_paise": 25}],
                "total_inr": "0.25",
            },
            "/metrics/reconciliation": {"status": "healthy"},
        }
        return responses[path]


def test_render_covers_all_monitoring_pages_without_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StreamlitStub()
    monkeypatch.setattr(dashboard, "st", stub)
    dashboard.render(ClientStub())  # type: ignore[arg-type]
    assert stub.errors == []
    assert dashboard.DecimalPaise("125") == 1.25


def test_render_and_control_errors_are_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StreamlitStub()
    monkeypatch.setattr(dashboard, "st", stub)

    class FailingClient:
        def get(self, _path: str) -> dict[str, Any]:
            raise DashboardError("offline")

    dashboard.render(FailingClient())  # type: ignore[arg-type]
    dashboard._show_control(lambda: (_ for _ in ()).throw(DashboardError("denied")))
    assert stub.errors == ["offline", "denied"]

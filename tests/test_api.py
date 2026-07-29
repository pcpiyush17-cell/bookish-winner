from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from personal_quant.api import ApiServerConfig, ControlAuthenticator, create_app
from personal_quant.clocks import SimulatedClock
from personal_quant.risk import KillSwitch
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner

NOW = datetime(2026, 7, 29, 5, tzinfo=UTC)
TOKEN = "local-control-token-that-is-at-least-32-characters"


class Shutdown:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def request_shutdown(self, reason: str) -> bool:
        self.reasons.append(reason)
        return True


def setup(tmp_path: Path) -> tuple[Database, Shutdown, TestClient]:
    database = Database(tmp_path / "trading.sqlite")
    MigrationRunner(database).apply_all()
    shutdown = Shutdown()
    app = create_app(
        database,
        ControlAuthenticator.from_token(TOKEN),
        clock=SimulatedClock(NOW),
        shutdown_controller=shutdown,
    )
    return database, shutdown, TestClient(app)


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "X-Control-Actor": "piyush"}


def test_read_only_metrics_cover_system_capital_orders_risk_costs_and_reconciliation(
    tmp_path: Path,
) -> None:
    database, _, client = setup(tmp_path)
    connection = database.connect(read_only=True)
    try:
        audit_before = connection.execute("SELECT count(*) FROM control_audit").fetchone()[0]
    finally:
        connection.close()
    assert client.get("/health").json() == {
        "status": "ok",
        "scope": "localhost",
        "live_enable_available": False,
    }
    for path in (
        "/metrics/system",
        "/metrics/capital",
        "/metrics/orders",
        "/metrics/risk",
        "/metrics/costs",
        "/metrics/reconciliation",
        "/audit/controls",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
    system = client.get("/metrics/system").json()
    assert system["wp14_operational_acceptance"]["status"] == "pending"
    assert not system["kill_switch_active"]
    connection = database.connect(read_only=True)
    try:
        audit_after = connection.execute("SELECT count(*) FROM control_audit").fetchone()[0]
    finally:
        connection.close()
    assert audit_after == audit_before
    assert client.post("/controls/live/enable").status_code == 404


def test_controls_require_auth_confirmation_and_record_every_attempt(tmp_path: Path) -> None:
    database, shutdown, client = setup(tmp_path)
    payload = {"reason": "operator safety stop", "confirmation": "ACTIVATE KILL SWITCH"}
    denied = client.post("/controls/kill-switch/activate", json=payload)
    assert denied.status_code == 401
    assert not KillSwitch(database).active()

    invalid = client.post(
        "/controls/kill-switch/activate",
        headers=auth(),
        json={**payload, "confirmation": "yes"},
    )
    assert invalid.status_code == 409
    activated = client.post("/controls/kill-switch/activate", headers=auth(), json=payload)
    assert activated.status_code == 200
    assert activated.json() == {"active": True, "changed": True}
    assert KillSwitch(database).active()

    reset = client.post(
        "/controls/kill-switch/reset",
        headers=auth(),
        json={
            "reason": "review complete",
            "reconciliation_healthy": True,
            "confirmation": "RESET KILL SWITCH",
        },
    )
    assert reset.status_code == 200
    assert not KillSwitch(database).active()
    stopped = client.post(
        "/controls/runtime/stop",
        headers=auth(),
        json={"reason": "scheduled close", "confirmation": "STOP PAPER RUNTIME"},
    )
    assert stopped.status_code == 200
    assert shutdown.reasons == ["scheduled close"]

    entries = client.get("/audit/controls").json()["entries"]
    assert [entry["outcome"] for entry in reversed(entries)] == [
        "denied",
        "failed",
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    serialized = str(entries)
    assert TOKEN not in serialized
    assert all(entry["actor"] in {"unknown", "piyush"} for entry in entries)


def test_loopback_and_token_configuration_fail_closed() -> None:
    assert ApiServerConfig().host == "127.0.0.1"
    try:
        ApiServerConfig(host="0.0.0.0")
    except ValueError as error:
        assert getattr(error, "code", "") == "api_bind_unsafe"
    else:
        raise AssertionError("unsafe bind was accepted")
    try:
        ControlAuthenticator.from_token("short")
    except ValueError as error:
        assert getattr(error, "code", "") == "control_token_weak"
    else:
        raise AssertionError("weak token was accepted")

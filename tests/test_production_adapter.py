import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

import personal_quant.broker.production as production
from personal_quant.accounting import PortfolioAccounting
from personal_quant.broker.auth import BrokerAuthenticationError, TokenStore
from personal_quant.broker.contracts import (
    BrokerCancelRequest,
    BrokerError,
    BrokerModifyRequest,
    BrokerOrderRequest,
    BrokerOrderStatus,
    OrderSide,
)
from personal_quant.broker.production import (
    PRODUCTION_ROOT,
    ProductionAuthenticator,
    ProductionKiteAdapter,
    ProductionSafetyConfig,
    create_production_client,
)
from personal_quant.clocks import SimulatedClock
from personal_quant.domain.identifiers import ClientOrderId, InstrumentKey
from personal_quant.domain.money import Money
from personal_quant.paper_runtime import RuntimeProgress
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner

NOW = datetime(2026, 7, 29, 10, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "broker"


class FakeProductionClient:
    def __init__(self) -> None:
        self._routes: dict[str, str] = {}
        self._default_login_uri = "https://kite.zerodha.com/connect/login"
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.user_id = "AB1234"

    def login_url(self) -> str:
        return f"{self._default_login_uri}?api_key=fixture"

    def generate_session(self, request_token: str, api_secret: str) -> dict[str, Any]:
        self.calls.append(
            ("generate_session", {"request_token": request_token, "api_secret": api_secret})
        )
        return {"access_token": "production-access-token"}

    def set_access_token(self, access_token: str) -> None:
        self.calls.append(("set_access_token", {"access_token": access_token}))

    def profile(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "user_name": "Piyush Chandra",
            "broker": "ZERODHA",
            "exchanges": ["NSE"],
            "products": ["CNC"],
        }

    def margins(self, segment: str | None = None) -> dict[str, Any]:
        return {"net": 10000}

    def positions(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "net": [
                {
                    "exchange": "NSE",
                    "tradingsymbol": "HDFCBANK",
                    "quantity": 2,
                    "average_price": 1649.5,
                }
            ]
        }

    def holdings(self) -> list[dict[str, Any]]:
        return self.positions()["net"]

    def orders(self) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            json.loads((FIXTURES / "sandbox_orders.json").read_text(encoding="utf-8")),
        )

    def trades(self) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            json.loads((FIXTURES / "sandbox_trades.json").read_text(encoding="utf-8")),
        )

    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        return []

    def historical_data(
        self,
        instrument_token: int,
        from_date: datetime | str,
        to_date: datetime | str,
        interval: str,
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict[str, Any]]:
        return []

    def place_order(self, **parameters: object) -> str:
        self.calls.append(("place_order", parameters))
        return "production-order-1"

    def modify_order(self, **parameters: object) -> str:
        self.calls.append(("modify_order", parameters))
        return str(parameters["order_id"])

    def cancel_order(self, **parameters: object) -> str:
        self.calls.append(("cancel_order", parameters))
        return str(parameters["order_id"])


def database(tmp_path: Path) -> Database:
    result = Database(tmp_path / "trading.sqlite")
    MigrationRunner(result).apply_all()
    return result


def request() -> BrokerOrderRequest:
    return BrokerOrderRequest(
        ClientOrderId(str(uuid4())),
        InstrumentKey("NSE:HDFCBANK"),
        OrderSide.BUY,
        1,
        Money.from_value("1650.25"),
        "pqlive01",
    )


def test_production_auth_validates_identity_and_records_next_expiry(tmp_path: Path) -> None:
    client = FakeProductionClient()
    authenticator = ProductionAuthenticator(
        client, SimulatedClock(NOW), TokenStore(tmp_path / "token.json")
    )

    session = authenticator.exchange(
        request_token="one-use", api_secret="secret", expected_user_id="AB1234"
    )

    assert session.profile.broker == "ZERODHA"
    assert session.expires_at > NOW
    assert Path(session.token_path).exists()
    assert "production-access-token" not in repr(session)


def test_production_auth_rejects_wrong_account_without_storing_token(tmp_path: Path) -> None:
    client = FakeProductionClient()
    client.user_id = "WRONG"
    target = tmp_path / "token.json"
    authenticator = ProductionAuthenticator(client, SimulatedClock(NOW), TokenStore(target))

    with pytest.raises(BrokerAuthenticationError) as captured:
        authenticator.exchange(
            request_token="one-use", api_secret="secret", expected_user_id="AB1234"
        )

    assert captured.value.code == "production_identity_mismatch"
    assert not target.exists()


def test_production_orders_are_closed_by_default_and_no_sdk_write_occurs(tmp_path: Path) -> None:
    client = FakeProductionClient()
    adapter = ProductionKiteAdapter(
        client,
        SimulatedClock(NOW),
        database(tmp_path),
        ProductionSafetyConfig("AB1234", "198.51.100.10"),
        lambda: "198.51.100.10",
    )

    preflight = adapter.preflight()
    with pytest.raises(BrokerError) as captured:
        adapter.place_order(request())

    assert preflight.passed is False
    assert preflight.checks["feature_gate"] is False
    assert captured.value.code == "production_order_gate_closed"
    assert all(name != "place_order" for name, _ in client.calls)


def test_all_gates_allow_limit_cnc_mapping_with_fake_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database(tmp_path)
    with db.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO shadow_reports VALUES (?, ?, ?, 0, 0, '{}', '[]', ?, ?)",
            (str(uuid4()), NOW.isoformat(), "AB1234", "shadow.json", "a" * 64),
        )
    monkeypatch.setattr(
        production,
        "runtime_progress",
        lambda _database: RuntimeProgress(10, 30, True, True),
    )
    client = FakeProductionClient()
    adapter = ProductionKiteAdapter(
        client,
        SimulatedClock(NOW),
        db,
        ProductionSafetyConfig("AB1234", "198.51.100.10", order_routing_enabled=True),
        lambda: "198.51.100.10",
    )

    assert adapter.preflight().passed is True
    acknowledgement = adapter.place_order(request())
    adapter.modify_order(
        BrokerModifyRequest(acknowledgement.broker_order_id, limit_price=Money.from_value("1651"))
    )
    adapter.cancel_order(BrokerCancelRequest(acknowledgement.broker_order_id))
    parameters = next(value for name, value in client.calls if name == "place_order")

    assert parameters["order_type"] == "LIMIT"
    assert parameters["product"] == "CNC"
    assert parameters["variety"] == "regular"
    assert parameters["exchange"] == "NSE"


def test_read_models_order_updates_and_reconciliation_use_neutral_contracts(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    adapter = ProductionKiteAdapter(
        FakeProductionClient(),
        SimulatedClock(NOW),
        db,
        ProductionSafetyConfig("AB1234", "198.51.100.10"),
        lambda: "198.51.100.10",
    )

    assert adapter.get_profile().user_id == "AB1234"
    assert adapter.get_holdings()[0].quantity == 2
    assert adapter.get_orders()[0].status is BrokerOrderStatus.PARTIALLY_FILLED
    assert adapter.get_trades()[0].price == Money.from_value("1649.50")
    payload = json.loads((FIXTURES / "sandbox_orders.json").read_text(encoding="utf-8"))[0]
    assert (
        adapter.map_order_update(payload).broker_order_id == adapter.get_orders()[0].broker_order_id
    )
    differences = adapter.reconcile(PortfolioAccounting(db))
    assert {item.layer for item in differences} == {"cash", "positions"}


def test_invalid_ip_update_and_sdk_failures_fail_closed_and_redact(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="IPv4"):
        ProductionSafetyConfig("AB1234", "::1")
    client = FakeProductionClient()
    adapter = ProductionKiteAdapter(
        client,
        SimulatedClock(NOW),
        database(tmp_path),
        ProductionSafetyConfig("AB1234", "198.51.100.10"),
        lambda: "not-an-ip",
    )
    with pytest.raises(BrokerError) as ip_error:
        adapter.preflight()
    assert ip_error.value.code == "production_public_ip_unavailable"

    def fail() -> dict[str, Any]:
        raise RuntimeError("leaked-production-token")

    client.profile = fail  # type: ignore[method-assign]
    with pytest.raises(BrokerError) as sdk_error:
        adapter.get_profile()
    assert sdk_error.value.code == "production_profile_failed"
    assert "leaked-production-token" not in str(sdk_error.value)


def test_production_root_is_explicit_and_live_config_remains_disabled() -> None:
    assert PRODUCTION_ROOT == "https://api.kite.trade"
    client = create_production_client("fixture-key")
    assert cast(Any, client)._default_root_uri == PRODUCTION_ROOT
    config = Path("config/live.example.yaml").read_text(encoding="utf-8")
    assert "order_routing_enabled: false" in config

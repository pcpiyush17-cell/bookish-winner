"""Feature-gated Zerodha production adapter.

The adapter compiles the complete broker contract, but order transmission remains disabled
unless every explicit runtime, identity, network, paper, and shadow gate passes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from ipaddress import IPv4Address, ip_address
from typing import Any, TypeVar, cast
from zoneinfo import ZoneInfo

from personal_quant.accounting import PortfolioAccounting, ReconciliationDifference
from personal_quant.clocks import Clock
from personal_quant.domain.identifiers import BrokerOrderId
from personal_quant.paper_runtime import runtime_progress
from personal_quant.storage.database import Database

from .auth import BrokerAuthenticationError, TokenStore, _required_string
from .contracts import (
    BrokerCancelRequest,
    BrokerError,
    BrokerHolding,
    BrokerModifyRequest,
    BrokerOrder,
    BrokerOrderAck,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerProfile,
    BrokerTrade,
    FundsSnapshot,
    split_instrument,
)
from .rate_limit import BrokerRateLimiter
from .sandbox import KiteClient, _holding, _order, _position, _trade

PRODUCTION_ROOT = "https://api.kite.trade"
_INDIA = ZoneInfo("Asia/Kolkata")
_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class ProductionSession:
    profile: BrokerProfile
    token_path: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ProductionAuthenticator:
    client: KiteClient
    clock: Clock
    token_store: TokenStore

    def login_url(self) -> str:
        return self.client.login_url()

    def exchange(
        self, *, request_token: str, api_secret: str, expected_user_id: str
    ) -> ProductionSession:
        try:
            session = self.client.generate_session(request_token, api_secret=api_secret)
            access_token = _required_string(session, "access_token")
            self.client.set_access_token(access_token)
            profile = _profile(self.client.profile())
        except BrokerAuthenticationError:
            raise
        except Exception:
            raise BrokerAuthenticationError(
                "production_auth_failed",
                "Production authentication failed; credentials were redacted",
            ) from None
        if profile.user_id != expected_user_id or profile.broker.upper() != "ZERODHA":
            raise BrokerAuthenticationError(
                "production_identity_mismatch",
                "Authenticated production account does not match the approved identity",
            )
        authenticated_at = self.clock.now()
        token_path = self.token_store.save(
            access_token=access_token,
            user_id=profile.user_id,
            authenticated_at=authenticated_at.isoformat(),
        )
        return ProductionSession(profile, str(token_path), _next_token_expiry(authenticated_at))


def create_production_client(api_key: str) -> KiteClient:
    """Create the official SDK client with its production root."""
    from kiteconnect import KiteConnect  # type: ignore[import-untyped]

    return cast(KiteClient, KiteConnect(api_key=api_key, root=PRODUCTION_ROOT))


@dataclass(frozen=True, slots=True)
class ProductionSafetyConfig:
    expected_user_id: str
    expected_public_ip: str
    order_routing_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.expected_user_id.strip():
            raise ValueError("expected_user_id is required")
        parsed = ip_address(self.expected_public_ip)
        if not isinstance(parsed, IPv4Address):
            raise ValueError("expected_public_ip must be an IPv4 address")


@dataclass(frozen=True, slots=True)
class ProductionPreflight:
    passed: bool
    checks: dict[str, bool]
    checked_at: datetime


class ProductionKiteAdapter:
    """Map the official production SDK behind mandatory fail-closed live gates."""

    def __init__(
        self,
        client: KiteClient,
        clock: Clock,
        database: Database,
        config: ProductionSafetyConfig,
        current_public_ip: Callable[[], str],
    ) -> None:
        self._client = client
        self._clock = clock
        self._database = database
        self._config = config
        self._current_public_ip = current_public_ip
        self._limiter = BrokerRateLimiter(clock)
        self._authorised = False

    def preflight(self) -> ProductionPreflight:
        profile = self.get_profile()
        progress = runtime_progress(self._database)
        current_ip = self._read_public_ip()
        checks = {
            "feature_gate": self._config.order_routing_enabled,
            "account_identity": profile.user_id == self._config.expected_user_id,
            "broker_identity": profile.broker.upper() == "ZERODHA",
            "nse_enabled": "NSE" in profile.exchanges,
            "cnc_enabled": "CNC" in profile.products,
            "public_ip": current_ip == self._config.expected_public_ip,
            "wp14_dry_acceptance": progress.dry_requirement_met,
            "wp14_formal_acceptance": progress.formal_requirement_met,
            "shadow_reconciliation": _shadow_gate_passed(self._database),
        }
        self._authorised = all(checks.values())
        return ProductionPreflight(self._authorised, checks, self._clock.now())

    def get_profile(self) -> BrokerProfile:
        return _profile(self._safe_call("profile", self._client.profile))

    def get_funds(self) -> FundsSnapshot:
        raw = self._safe_call("funds", lambda: self._client.margins("equity"))
        from personal_quant.domain.money import Money

        return FundsSnapshot(Money.from_value(str(raw["net"])))

    def get_positions(self) -> tuple[BrokerPosition, ...]:
        raw = self._safe_call("positions", self._client.positions)
        return tuple(_position(item) for item in raw.get("net", []))

    def get_holdings(self) -> tuple[BrokerHolding, ...]:
        return tuple(_holding(item) for item in self._safe_call("holdings", self._client.holdings))

    def get_orders(self) -> tuple[BrokerOrder, ...]:
        return tuple(_order(item) for item in self._safe_call("orders", self._client.orders))

    def get_trades(self) -> tuple[BrokerTrade, ...]:
        return tuple(_trade(item) for item in self._safe_call("trades", self._client.trades))

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderAck:
        self._require_order_gate()
        self._limiter.acquire_new_order()
        exchange, symbol = split_instrument(request.instrument)
        order_id = self._safe_call(
            "place_order",
            lambda: self._client.place_order(
                variety="regular",
                exchange=exchange,
                tradingsymbol=symbol,
                transaction_type=request.side.value,
                quantity=request.quantity,
                product="CNC",
                order_type="LIMIT",
                price=str(request.limit_price.amount),
                validity="DAY",
                tag=request.tag,
            ),
        )
        return BrokerOrderAck(BrokerOrderId(str(order_id)), BrokerOrderStatus.OPEN)

    def modify_order(self, request: BrokerModifyRequest) -> BrokerOrderAck:
        self._require_order_gate()
        self._limiter.acquire_modification(request.broker_order_id)
        parameters: dict[str, object] = {
            "variety": "regular",
            "order_id": str(request.broker_order_id),
            "order_type": "LIMIT",
        }
        if request.quantity is not None:
            parameters["quantity"] = request.quantity
        if request.limit_price is not None:
            parameters["price"] = str(request.limit_price.amount)
        order_id = self._safe_call("modify_order", lambda: self._client.modify_order(**parameters))
        return BrokerOrderAck(BrokerOrderId(str(order_id)), BrokerOrderStatus.OPEN)

    def cancel_order(self, request: BrokerCancelRequest) -> BrokerOrderAck:
        self._require_order_gate()
        self._limiter.record_cancellation()
        order_id = self._safe_call(
            "cancel_order",
            lambda: self._client.cancel_order(
                variety="regular", order_id=str(request.broker_order_id)
            ),
        )
        return BrokerOrderAck(BrokerOrderId(str(order_id)), BrokerOrderStatus.CANCELLED)

    def map_order_update(self, payload: dict[str, Any]) -> BrokerOrder:
        """Map one official SDK order-update payload to the broker-neutral model."""
        try:
            return _order(payload)
        except Exception:
            raise BrokerError(
                "production_order_update_invalid", "Production order update was invalid"
            ) from None

    def reconcile(self, accounting: PortfolioAccounting) -> tuple[ReconciliationDifference, ...]:
        funds = self.get_funds()
        positions = {
            item.instrument: (item.quantity, item.average_price) for item in self.get_positions()
        }
        return accounting.reconcile_cash(funds.available_cash) + accounting.reconcile_positions(
            positions
        )

    def _require_order_gate(self) -> None:
        if (
            not self._authorised
            or not self._config.order_routing_enabled
            or self._read_public_ip() != self._config.expected_public_ip
        ):
            self._authorised = False
            raise BrokerError(
                "production_order_gate_closed",
                "Production order routing is disabled or its safety pre-flight is stale",
            )

    def _read_public_ip(self) -> str:
        try:
            value = str(ip_address(self._current_public_ip().strip()))
        except (ValueError, OSError):
            raise BrokerError(
                "production_public_ip_unavailable", "Current public IP could not be verified"
            ) from None
        return value

    @staticmethod
    def _safe_call(operation: str, call: Callable[[], _Result]) -> _Result:
        try:
            return call()
        except Exception:
            raise BrokerError(
                f"production_{operation}_failed",
                f"Production {operation} failed; sensitive details were redacted",
            ) from None


def _profile(raw: dict[str, Any]) -> BrokerProfile:
    return BrokerProfile(
        user_id=_required_string(raw, "user_id"),
        user_name=_required_string(raw, "user_name"),
        broker=_required_string(raw, "broker"),
        exchanges=tuple(str(value) for value in raw["exchanges"]),
        products=tuple(str(value) for value in raw["products"]),
    )


def _shadow_gate_passed(database: Database) -> bool:
    connection = database.connect(read_only=True)
    try:
        row = connection.execute(
            """
            SELECT difference_count FROM shadow_reports
            ORDER BY captured_at DESC, report_id DESC LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    return row is not None and int(row["difference_count"]) == 0


def _next_token_expiry(authenticated_at: datetime) -> datetime:
    local = authenticated_at.astimezone(_INDIA)
    candidate = datetime.combine(local.date(), time(6), _INDIA)
    if local >= candidate:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)

"""Localhost-only monitoring API and authenticated, audited safety controls."""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from personal_quant.clocks import Clock, SystemClock
from personal_quant.paper_runtime import runtime_progress
from personal_quant.risk import KillSwitch, RiskError
from personal_quant.storage.database import Database


class ApiError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ShutdownController(Protocol):
    def request_shutdown(self, reason: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ApiServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765

    def __post_init__(self) -> None:
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ApiError("api_bind_unsafe", "Local API must bind to a loopback host")
        if not 1 <= self.port <= 65535:
            raise ApiError("api_port_invalid", "API port is outside the valid range")


@dataclass(frozen=True, slots=True)
class ControlAuthenticator:
    token_digest: str

    @classmethod
    def from_token(cls, token: str) -> ControlAuthenticator:
        if len(token) < 32:
            raise ApiError(
                "control_token_weak", "Control token must contain at least 32 characters"
            )
        return cls(hashlib.sha256(token.encode()).hexdigest())

    def valid(self, authorization: str | None) -> bool:
        if authorization is None or not authorization.startswith("Bearer "):
            return False
        candidate = hashlib.sha256(authorization.removeprefix("Bearer ").encode()).hexdigest()
        return secrets.compare_digest(candidate, self.token_digest)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ActivateKillSwitchRequest(StrictRequest):
    reason: str = Field(min_length=3, max_length=500)
    confirmation: str


class ResetKillSwitchRequest(StrictRequest):
    reason: str = Field(min_length=3, max_length=500)
    reconciliation_healthy: bool
    confirmation: str


class RuntimeStopRequest(StrictRequest):
    reason: str = Field(min_length=3, max_length=500)
    confirmation: str


@dataclass(frozen=True, slots=True)
class MetricsReader:
    database: Database

    def system(self) -> dict[str, object]:
        connection = self.database.connect(read_only=True)
        try:
            session = connection.execute(
                """
                SELECT session_id, evidence_kind, state, started_at, ready_at, ended_at,
                       clean_shutdown, reconciliation_healthy, git_commit, failure_code
                FROM paper_runtime_sessions ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            migrations = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        progress = runtime_progress(self.database)
        return {
            "mode": "paper",
            "database_schema_version": migrations,
            "database_integrity": self.database.integrity_check().passed,
            "kill_switch_active": KillSwitch(self.database).active(),
            "latest_session": dict(session) if session is not None else None,
            "wp14_operational_acceptance": {
                "status": "pending",
                "dry_sessions": progress.successful_dry_sessions,
                "formal_sessions": progress.successful_formal_sessions,
                "dry_requirement_met": progress.dry_requirement_met,
                "formal_requirement_met": progress.formal_requirement_met,
            },
        }

    def capital(self) -> dict[str, object]:
        connection = self.database.connect(read_only=True)
        try:
            cash_paise = int(
                connection.execute(
                    "SELECT COALESCE(SUM(amount_paise), 0) FROM cash_ledger"
                ).fetchone()[0]
            )
            positions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM positions WHERE quantity != 0 ORDER BY instrument_key"
                ).fetchall()
            ]
            realised_paise = int(
                connection.execute(
                    "SELECT COALESCE(SUM(realised_pnl_paise), 0) FROM positions"
                ).fetchone()[0]
            )
            costs_paise = int(
                connection.execute(
                    "SELECT COALESCE(SUM(amount_paise), 0) FROM cost_entries"
                ).fetchone()[0]
            )
            valuation = connection.execute(
                """
                SELECT COALESCE(SUM(market_value_paise), 0) AS market_value_paise,
                       COALESCE(SUM(unrealised_pnl_paise), 0) AS unrealised_pnl_paise,
                       MAX(valued_at) AS valued_at
                FROM (
                    SELECT v.* FROM valuation_snapshots v
                    JOIN (
                        SELECT instrument_key, MAX(valued_at) AS latest
                        FROM valuation_snapshots GROUP BY instrument_key
                    ) x ON x.instrument_key=v.instrument_key AND x.latest=v.valued_at
                )
                """
            ).fetchone()
        finally:
            connection.close()
        market_paise = int(valuation["market_value_paise"])
        unrealised_paise = int(valuation["unrealised_pnl_paise"])
        return {
            "cash_inr": _rupees(cash_paise),
            "market_value_inr": _rupees(market_paise),
            "net_liquidation_value_inr": _rupees(cash_paise + market_paise),
            "realised_pnl_inr": _rupees(realised_paise),
            "unrealised_pnl_inr": _rupees(unrealised_paise),
            "variable_costs_inr": _rupees(costs_paise),
            "trading_net_pnl_inr": _rupees(realised_paise + unrealised_paise - costs_paise),
            "valued_at": valuation["valued_at"],
            "positions": positions,
        }

    def orders(self) -> dict[str, object]:
        connection = self.database.connect(read_only=True)
        try:
            orders = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM oms_orders ORDER BY created_at DESC LIMIT 200"
                ).fetchall()
            ]
            fills = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM oms_fills ORDER BY filled_at DESC LIMIT 200"
                ).fetchall()
            ]
        finally:
            connection.close()
        return {"orders": orders, "fills": fills}

    def risk(self) -> dict[str, object]:
        connection = self.database.connect(read_only=True)
        try:
            decisions = [
                dict(row)
                for row in connection.execute(
                    """
                SELECT decision_id, intent_id, decision, requested_quantity,
                       approved_quantity, reason_codes_json, config_hash, evaluated_at
                FROM risk_decisions ORDER BY evaluated_at DESC LIMIT 200
                """
                ).fetchall()
            ]
            incidents = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM oms_incidents ORDER BY created_at DESC LIMIT 100"
                ).fetchall()
            ]
        finally:
            connection.close()
        return {"decisions": decisions, "incidents": incidents}

    def costs(self) -> dict[str, object]:
        connection = self.database.connect(read_only=True)
        try:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                SELECT component, cost_kind, calculation_version,
                       SUM(amount_paise) AS amount_paise, COUNT(*) AS entries
                FROM cost_entries
                GROUP BY component, cost_kind, calculation_version
                ORDER BY component, cost_kind
                """
                ).fetchall()
            ]
        finally:
            connection.close()
        return {
            "components": rows,
            "total_inr": _rupees(sum(int(row["amount_paise"]) for row in rows)),
        }

    def reconciliation(self) -> dict[str, object]:
        connection = self.database.connect(read_only=True)
        try:
            session = connection.execute(
                """
                SELECT session_id, state, reconciliation_healthy, clean_shutdown,
                       ended_at, failure_code
                FROM paper_runtime_sessions ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            snapshots = [
                dict(row)
                for row in connection.execute(
                    """
                SELECT snapshot_kind, created_at, payload_json FROM runtime_snapshots
                ORDER BY created_at DESC LIMIT 20
                """
                ).fetchall()
            ]
            unknown_orders = int(
                connection.execute(
                    """
                SELECT count(*) FROM oms_orders
                WHERE state IN ('UNKNOWN', 'RECONCILIATION_REQUIRED')
                """
                ).fetchone()[0]
            )
        finally:
            connection.close()
        return {
            "latest_session": dict(session) if session is not None else None,
            "unknown_orders": unknown_orders,
            "snapshots": snapshots,
        }

    def audit(self) -> dict[str, object]:
        connection = self.database.connect(read_only=True)
        try:
            entries = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM control_audit ORDER BY occurred_at DESC LIMIT 200"
                ).fetchall()
            ]
        finally:
            connection.close()
        return {"entries": entries}


@dataclass(frozen=True, slots=True)
class ControlService:
    database: Database
    clock: Clock
    shutdown_controller: ShutdownController | None = None

    def denied(self, actor: str, action: str) -> None:
        self._audit(actor, action, "denied", "authentication_failed")

    def activate_kill_switch(self, actor: str, reason: str, confirmation: str) -> dict[str, object]:
        action = "kill_switch_activate"
        if confirmation != "ACTIVATE KILL SWITCH":
            self._audit(actor, action, "failed", "confirmation_invalid")
            raise ApiError("confirmation_invalid", "Exact kill-switch confirmation is required")
        try:
            changed = KillSwitch(self.database).activate(reason, self.clock.now())
        except RiskError as error:
            self._audit(actor, action, "failed", error.code)
            raise ApiError(error.code, str(error)) from error
        self._audit(actor, action, "succeeded", _safe_reason(reason))
        return {"active": True, "changed": changed}

    def reset_kill_switch(
        self,
        actor: str,
        reason: str,
        reconciliation_healthy: bool,
        confirmation: str,
    ) -> dict[str, object]:
        action = "kill_switch_reset"
        if confirmation != "RESET KILL SWITCH":
            self._audit(actor, action, "failed", "confirmation_invalid")
            raise ApiError("confirmation_invalid", "Exact reset confirmation is required")
        try:
            KillSwitch(self.database).reset(
                self.clock.now(),
                reconciliation_healthy=reconciliation_healthy,
                human_authorised=True,
            )
        except RiskError as error:
            self._audit(actor, action, "failed", error.code)
            raise ApiError(error.code, str(error)) from error
        self._audit(actor, action, "succeeded", _safe_reason(reason))
        return {"active": False}

    def stop_runtime(self, actor: str, reason: str, confirmation: str) -> dict[str, object]:
        action = "runtime_stop"
        if confirmation != "STOP PAPER RUNTIME":
            self._audit(actor, action, "failed", "confirmation_invalid")
            raise ApiError("confirmation_invalid", "Exact runtime-stop confirmation is required")
        if self.shutdown_controller is None:
            self._audit(actor, action, "failed", "runtime_unavailable")
            raise ApiError("runtime_unavailable", "Runtime control channel is unavailable")
        if not self.shutdown_controller.request_shutdown(reason):
            self._audit(actor, action, "failed", "runtime_stop_rejected")
            raise ApiError("runtime_stop_rejected", "Runtime rejected the shutdown request")
        self._audit(actor, action, "succeeded", _safe_reason(reason))
        return {"shutdown_requested": True}

    def _audit(self, actor: str, action: str, outcome: str, reason: str) -> None:
        safe_actor = actor.strip()[:100] or "unknown"
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO control_audit VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    safe_actor,
                    action,
                    outcome,
                    reason[:500],
                    self.clock.now().astimezone(UTC).isoformat(),
                ),
            )


def create_app(
    database: Database,
    authenticator: ControlAuthenticator,
    *,
    clock: Clock | None = None,
    shutdown_controller: ShutdownController | None = None,
) -> FastAPI:
    reader = MetricsReader(database)
    controls = ControlService(database, clock or SystemClock(), shutdown_controller)
    app = FastAPI(title="Personal Quant Local API", version="1.0.0")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "scope": "localhost", "live_enable_available": False}

    @app.get("/metrics/system")
    def system_metrics() -> dict[str, object]:
        return reader.system()

    @app.get("/metrics/capital")
    def capital_metrics() -> dict[str, object]:
        return reader.capital()

    @app.get("/metrics/orders")
    def order_metrics() -> dict[str, object]:
        return reader.orders()

    @app.get("/metrics/risk")
    def risk_metrics() -> dict[str, object]:
        return reader.risk()

    @app.get("/metrics/costs")
    def cost_metrics() -> dict[str, object]:
        return reader.costs()

    @app.get("/metrics/reconciliation")
    def reconciliation_metrics() -> dict[str, object]:
        return reader.reconciliation()

    @app.get("/audit/controls")
    def control_audit() -> dict[str, object]:
        return reader.audit()

    def require_control(authorization: str | None, actor: str, action: str) -> None:
        if not authenticator.valid(authorization):
            controls.denied(actor, action)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Control authentication failed")

    @app.post("/controls/kill-switch/activate")
    def activate(
        request: ActivateKillSwitchRequest,
        authorization: str | None = Header(default=None),
        x_control_actor: str = Header(default="unknown"),
    ) -> dict[str, object]:
        require_control(authorization, x_control_actor, "kill_switch_activate")
        return _control_call(
            lambda: controls.activate_kill_switch(
                x_control_actor, request.reason, request.confirmation
            )
        )

    @app.post("/controls/kill-switch/reset")
    def reset(
        request: ResetKillSwitchRequest,
        authorization: str | None = Header(default=None),
        x_control_actor: str = Header(default="unknown"),
    ) -> dict[str, object]:
        require_control(authorization, x_control_actor, "kill_switch_reset")
        return _control_call(
            lambda: controls.reset_kill_switch(
                x_control_actor,
                request.reason,
                request.reconciliation_healthy,
                request.confirmation,
            )
        )

    @app.post("/controls/runtime/stop")
    def stop_runtime(
        request: RuntimeStopRequest,
        authorization: str | None = Header(default=None),
        x_control_actor: str = Header(default="unknown"),
    ) -> dict[str, object]:
        require_control(authorization, x_control_actor, "runtime_stop")
        return _control_call(
            lambda: controls.stop_runtime(x_control_actor, request.reason, request.confirmation)
        )

    return app


def run_server(
    database: Database,
    token: str,
    config: ApiServerConfig | None = None,
) -> None:
    selected = config or ApiServerConfig()
    uvicorn.run(
        create_app(database, ControlAuthenticator.from_token(token)),
        host=selected.host,
        port=selected.port,
        log_level="info",
    )


def main() -> None:
    token = os.environ.get("PQ_DASHBOARD_CONTROL_TOKEN", "")
    path = Path(os.environ.get("PQ_DATABASE_PATH", "state/trading.sqlite"))
    run_server(Database(path), token)


def _control_call(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
    try:
        return operation()
    except ApiError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{error.code}: {error}") from error


def _safe_reason(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ApiError("control_reason_missing", "Control reason is required")
    return normalized[:500]


def _rupees(paise: int) -> str:
    return str((Decimal(paise) / 100).quantize(Decimal("0.01")))


if __name__ == "__main__":
    main()

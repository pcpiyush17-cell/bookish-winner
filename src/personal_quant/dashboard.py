"""Streamlit dashboard that communicates only with the local HTTP API."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import plotly.graph_objects as go  # type: ignore[import-untyped]
import streamlit as st


class DashboardError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DashboardClient:
    base_url: str = "http://127.0.0.1:8765"
    timeout_seconds: float = 3.0

    def get(self, path: str) -> dict[str, Any]:
        return self._request(path, method="GET")

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        token: str,
        actor: str,
    ) -> dict[str, Any]:
        return self._request(
            path,
            method="POST",
            payload=payload,
            headers={"Authorization": f"Bearer {token}", "X-Control-Actor": actor},
        )

    def _request(
        self,
        path: str,
        *,
        method: str,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise DashboardError("Dashboard API URL must be loopback-only")
        body = json.dumps(payload).encode() if payload is not None else None
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        request = Request(
            f"{self.base_url.rstrip('/')}/{path.lstrip('/')}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read())
                if not isinstance(result, dict):
                    raise DashboardError("Local API returned an invalid document")
                return result
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise DashboardError("Local API is unavailable or rejected the request") from error


def render(client: DashboardClient) -> None:
    st.set_page_config(page_title="Personal Quant", layout="wide")
    st.title("Personal Quant — Local Monitor")
    st.caption("Monitoring is read-only. Controls call the authenticated localhost API.")
    try:
        system = client.get("/metrics/system")
    except DashboardError as error:
        st.error(str(error))
        return

    latest = system.get("latest_session") or {}
    acceptance = system["wp14_operational_acceptance"]
    columns = st.columns(4)
    columns[0].metric("Mode", system["mode"])
    columns[1].metric("Runtime", latest.get("state", "not started"))
    columns[2].metric("Kill switch", "ACTIVE" if system["kill_switch_active"] else "inactive")
    columns[3].metric("WP-14", acceptance["status"])
    st.progress(min(1.0, float(acceptance["dry_sessions"]) / 10), text="Dry sessions")
    st.progress(min(1.0, float(acceptance["formal_sessions"]) / 30), text="Formal sessions")

    (
        system_tab,
        capital_tab,
        orders_tab,
        strategy_tab,
        risk_tab,
        costs_tab,
        recon_tab,
        controls_tab,
    ) = st.tabs(
        [
            "System",
            "Capital & P&L",
            "Orders & fills",
            "Strategy",
            "Risk",
            "Costs",
            "Reconciliation",
            "Controls",
        ]
    )
    with system_tab:
        st.json(system)
    with capital_tab:
        capital = client.get("/metrics/capital")
        metrics = st.columns(4)
        metrics[0].metric("Cash", f"₹{capital['cash_inr']}")
        metrics[1].metric("Market value", f"₹{capital['market_value_inr']}")
        metrics[2].metric("Net liquidation", f"₹{capital['net_liquidation_value_inr']}")
        metrics[3].metric("Trading net P&L", f"₹{capital['trading_net_pnl_inr']}")
        st.dataframe(capital["positions"], use_container_width=True)
    with orders_tab:
        order_data = client.get("/metrics/orders")
        st.subheader("Orders")
        st.dataframe(order_data["orders"], use_container_width=True)
        st.subheader("Fills")
        st.dataframe(order_data["fills"], use_container_width=True)
    with strategy_tab:
        st.info(
            "baseline_momentum_v1 — engineering validation only; "
            "no live approval or profitability claim."
        )
        st.json({"wp14_operational_acceptance": acceptance})
    with risk_tab:
        risk = client.get("/metrics/risk")
        st.subheader("Risk decisions")
        st.dataframe(risk["decisions"], use_container_width=True)
        st.subheader("Incidents")
        st.dataframe(risk["incidents"], use_container_width=True)
    with costs_tab:
        costs = client.get("/metrics/costs")
        figure = go.Figure(
            data=[
                go.Bar(
                    x=[item["component"] for item in costs["components"]],
                    y=[DecimalPaise(item["amount_paise"]) for item in costs["components"]],
                )
            ]
        )
        figure.update_layout(title="Costs by component", yaxis_title="INR")
        st.plotly_chart(figure, use_container_width=True)
        st.metric("Total variable costs", f"₹{costs['total_inr']}")
    with recon_tab:
        st.json(client.get("/metrics/reconciliation"))
    with controls_tab:
        _render_controls(client)


def _render_controls(client: DashboardClient) -> None:
    st.warning("Controls are audited and require the control token plus an exact confirmation.")
    actor = st.text_input("Operator name")
    token = st.text_input("Control token", type="password")
    reason = st.text_input("Reason")
    activate_confirmation = st.text_input("Type ACTIVATE KILL SWITCH")
    if st.button("Activate kill switch", type="primary"):
        _show_control(
            lambda: client.post(
                "/controls/kill-switch/activate",
                {"reason": reason, "confirmation": activate_confirmation},
                token=token,
                actor=actor,
            )
        )
    reset_confirmation = st.text_input("Type RESET KILL SWITCH")
    reconciled = st.checkbox("Reconciliation is healthy")
    if st.button("Reset kill switch"):
        _show_control(
            lambda: client.post(
                "/controls/kill-switch/reset",
                {
                    "reason": reason,
                    "reconciliation_healthy": reconciled,
                    "confirmation": reset_confirmation,
                },
                token=token,
                actor=actor,
            )
        )
    stop_confirmation = st.text_input("Type STOP PAPER RUNTIME")
    if st.button("Request graceful paper-runtime stop"):
        _show_control(
            lambda: client.post(
                "/controls/runtime/stop",
                {"reason": reason, "confirmation": stop_confirmation},
                token=token,
                actor=actor,
            )
        )


def _show_control(operation: Callable[[], dict[str, Any]]) -> None:
    try:
        st.success(operation())
    except DashboardError as error:
        st.error(str(error))


def DecimalPaise(value: object) -> float:
    return int(str(value)) / 100


def main() -> None:
    base_url = os.environ.get("PQ_LOCAL_API_URL", "http://127.0.0.1:8765")
    render(DashboardClient(base_url))


if __name__ == "__main__":
    main()

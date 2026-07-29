# WP-17 production adapter safety contract

WP-17 compiles Zerodha production authentication, account/profile validation, public-IP
comparison, LIMIT/CNC order mapping, order-update mapping, and broker/local reconciliation. It
does not approve or enable live trading.

The checked-in [`config/live.example.yaml`](../config/live.example.yaml) keeps
`order_routing_enabled` false. The adapter rejects every place, modify, and cancel request until
one pre-flight has simultaneously verified the explicit feature flag, expected Zerodha user,
ZERODHA broker identity, NSE and CNC capabilities, current registered IPv4 address, WP-14 paper
acceptance, and a clean latest shadow comparison. It checks the public IP again for every write.

Production login is human-mediated:

```powershell
$env:KITE_API_KEY = "..."
$env:KITE_API_SECRET = "..."
$env:KITE_EXPECTED_USER_ID = "..."
uv run pq kite-production-login
uv run pq kite-production-login --exchange
```

Never commit those environment values or the resulting token file. Authentication alone cannot
enable orders. Tokens are treated as expiring no later than the next 06:00 Asia/Kolkata boundary.

Implementation assumptions were checked against Zerodha's official
[Kite Connect authentication and profile documentation](https://kite.trade/docs/connect/v3/user/)
and [order API documentation](https://kite.trade/docs/connect/v3/orders/). The initial order
policy remains regular NSE equity-delivery `LIMIT` orders using product `CNC`; market orders,
autoslice, iceberg, after-market automation, and all production execution remain unavailable by
default.

WP-14 operational acceptance remains pending. Do not change the live feature flag merely because
the adapter's engineering tests pass.

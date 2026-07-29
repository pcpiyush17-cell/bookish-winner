CREATE TABLE oms_orders (
    order_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    risk_decision_id TEXT NOT NULL REFERENCES risk_decisions(decision_id),
    instrument_key TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    requested_quantity INTEGER NOT NULL CHECK (requested_quantity > 0),
    approved_quantity INTEGER NOT NULL CHECK (approved_quantity >= 0),
    limit_price_paise INTEGER NOT NULL CHECK (limit_price_paise > 0),
    state TEXT NOT NULL CHECK (state IN (
        'CREATED', 'RISK_REJECTED', 'RISK_APPROVED', 'SUBMISSION_PENDING',
        'SUBMITTED', 'ACKNOWLEDGED', 'OPEN', 'PARTIALLY_FILLED', 'FILLED',
        'CANCEL_PENDING', 'CANCELLED', 'REJECTED', 'UNKNOWN',
        'RECONCILIATION_REQUIRED'
    )),
    client_order_id TEXT NOT NULL UNIQUE,
    broker_tag TEXT NOT NULL,
    broker_order_id TEXT UNIQUE,
    filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    average_price_paise INTEGER NOT NULL DEFAULT 0 CHECK (average_price_paise >= 0),
    modification_count INTEGER NOT NULL DEFAULT 0 CHECK (modification_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error_code TEXT,
    CHECK (filled_quantity <= approved_quantity)
) STRICT;

CREATE TABLE oms_state_events (
    event_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES oms_orders(order_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    occurred_at TEXT NOT NULL
) STRICT;

CREATE TABLE oms_fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES oms_orders(order_id),
    broker_order_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_paise INTEGER NOT NULL CHECK (price_paise > 0),
    filled_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64)
) STRICT;

CREATE TABLE oms_incidents (
    incident_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES oms_orders(order_id),
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
) STRICT;

CREATE INDEX oms_orders_state_idx ON oms_orders(state);
CREATE INDEX oms_state_events_order_idx ON oms_state_events(order_id, occurred_at);
CREATE INDEX oms_fills_order_idx ON oms_fills(order_id);

CREATE TABLE fills (
    fill_id TEXT PRIMARY KEY,
    broker_order_id TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_paise INTEGER NOT NULL CHECK (price_paise > 0),
    occurred_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK (
        length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'
    )
) STRICT;

CREATE TABLE positions (
    instrument_key TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    cost_basis_paise INTEGER NOT NULL CHECK (cost_basis_paise >= 0),
    realised_pnl_paise INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((quantity = 0 AND cost_basis_paise = 0) OR quantity > 0)
) STRICT;

CREATE TABLE cash_ledger (
    entry_id TEXT PRIMARY KEY,
    entry_type TEXT NOT NULL CHECK (entry_type IN (
        'opening_cash', 'deposit', 'withdrawal', 'purchase', 'sale', 'cost',
        'dividend', 'manual_adjustment', 'reversal'
    )),
    amount_paise INTEGER NOT NULL CHECK (amount_paise != 0),
    instrument_key TEXT,
    fill_id TEXT REFERENCES fills(fill_id),
    reversal_of TEXT REFERENCES cash_ledger(entry_id),
    occurred_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    CHECK (
        (entry_type = 'reversal' AND reversal_of IS NOT NULL)
        OR (entry_type != 'reversal' AND reversal_of IS NULL)
    )
) STRICT;

CREATE UNIQUE INDEX cash_ledger_one_reversal_idx
ON cash_ledger(reversal_of) WHERE reversal_of IS NOT NULL;

CREATE TABLE cost_entries (
    cost_entry_id TEXT PRIMARY KEY,
    journal_entry_id TEXT NOT NULL UNIQUE REFERENCES cash_ledger(entry_id),
    fill_id TEXT REFERENCES fills(fill_id),
    component TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    cost_kind TEXT NOT NULL CHECK (cost_kind IN ('estimate', 'actual', 'adjustment')),
    calculation_version TEXT NOT NULL,
    occurred_at TEXT NOT NULL
) STRICT;

CREATE TABLE valuation_snapshots (
    valuation_id TEXT PRIMARY KEY,
    instrument_key TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    mark_price_paise INTEGER NOT NULL CHECK (mark_price_paise > 0),
    market_value_paise INTEGER NOT NULL CHECK (market_value_paise >= 0),
    unrealised_pnl_paise INTEGER NOT NULL,
    valued_at TEXT NOT NULL
) STRICT;

CREATE INDEX fills_instrument_time_idx ON fills(instrument_key, occurred_at);
CREATE INDEX cash_ledger_time_idx ON cash_ledger(occurred_at);
CREATE INDEX cost_entries_fill_idx ON cost_entries(fill_id);

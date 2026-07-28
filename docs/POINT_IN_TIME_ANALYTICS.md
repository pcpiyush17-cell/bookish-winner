# Point-in-time analytics contract

Every analytics load requires an aware `as_of` timestamp. Rows after that instant are removed
before SQL queries or feature expressions run. Input manifests and curated Parquet checksums
must verify, and duplicate `(instrument_key, interval, timestamp)` rows fail the load.

Features are calculated independently within each instrument and interval after deterministic
timestamp sorting. A feature at candle time `t` may use that candle and earlier candles only;
it must never use a negative shift, a centred window, a backward as-of join, or a future-filled
value. A signal based on the completed candle at `t` is executable no earlier than the next
bar, as required by the blueprint's fill model.

Feature names are unique and definitions have explicit semantic versions. Each materialized
feature dataset records its cutoff, ordered feature names and versions, registry fingerprint,
input-manifest checksums, output checksum, and row count. Output Parquet paths are immutable.
Reproducing a result requires the same verified inputs, cutoff, feature versions, and code.

Gaps remain gaps. Analytics does not forward-fill prices across missing bars or sessions.

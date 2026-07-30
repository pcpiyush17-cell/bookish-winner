# WebSocket Reconnect Runbook

## Immediate automatic behavior

1. Mark the feed `RECONNECTING` and block new signals/orders through the feed-health gate.
2. Record the disconnect and start of the data gap in the immutable session stream.
3. Clear subscription and fresh-quote evidence from the disconnected generation.
4. Reconnect with bounded exponential backoff. Stop after the configured maximum attempts.
5. Resubscribe only to the approved token universe and restore the configured data mode.
6. Remain `AWAITING_FRESH_DATA` until every approved instrument has a valid current-generation
   quote within the maximum age.
7. Resume eligibility only after the feed reports `HEALTHY`. Connection alone is insufficient.

## Operator checks after repeated failure

- Confirm internet connectivity and that the PC did not sleep.
- Confirm the broker session is current without exposing tokens in logs or screenshots.
- Check the recorded lifecycle and data-quality events for the first failure.
- Confirm the instrument-token snapshot is current and the expected universe was subscribed.
- Do not bypass the health gate or manually mark the stream healthy.
- If open orders exist, reconcile broker orders and trades before allowing any new intent.
- If the recording stopped unexpectedly, verify its manifest and checksum before replay.

## Recovery evidence

Record the incident time, disconnect code, retry count, gap interval, restored subscriptions,
first fresh quote per instrument, reconciliation outcome, and operator decision. Use deterministic
replay to reproduce the recorded interval before closing a material incident.

## Kite tick identity

Kite ticks do not provide a broker sequence number. Their exchange timestamps can also repeat
within one second while price, volume, or depth changes. For these ticks, the collector uses the
canonical raw market payload as the identity discriminator: exact retransmissions remain
idempotent, while distinct same-second updates are accepted. Feeds that provide a broker sequence
retain the stricter instrument/timestamp/sequence identity, and a changed payload for the same
sequence is quarantined as a conflicting duplicate.

# QR-01 point-in-time NSE universe

QR-01 converts immutable Zerodha NSE equity instrument snapshots into a deterministic research
universe. Membership uses **exact snapshot** semantics: a security is eligible on a date only when
it appears as active in that date's validated snapshot. Missing dates are never filled from a later
snapshot, so a current constituent cannot leak backward into a historical experiment.

Each universe artifact records the source manifest path and checksum, full dated membership, and
additions/removals from the preceding observation. The data file and its manifest are immutable and
content-addressed. The quality policy rejects insufficient history, wrong exchange/segment/type,
excessive missing ISINs, duplicate ISINs, and a durable key that changes ISIN.

This is observation-time safety, not fabricated exchange history. Zerodha's instrument master does
not provide authoritative listing or delisting effective dates. Dates before the first stored
snapshot and gaps between snapshots are therefore unavailable. Research requiring those dates must
obtain a licensed historical constituent source and preserve its publication timestamps.

Build from the locally stored snapshots:

```powershell
uv run pq research-universe-build
```

Validate a previously generated artifact without changing it:

```powershell
uv run pq research-universe-check --manifest research/data/universes/<id>/manifest.json
```

These commands cannot route orders or modify WP-14 evidence.

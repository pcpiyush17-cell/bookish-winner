"""Fail-closed WebSocket collection, immutable recording, and deterministic replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4, uuid5

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from personal_quant.clocks import Clock, SimulatedClock
from personal_quant.domain.identifiers import InstrumentKey, InstrumentToken
from personal_quant.domain.money import Money

EVENT_NAMESPACE = UUID("35bda065-c574-4d06-89d8-bf654d6617ce")


class LiveDataError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class FeedState(StrEnum):
    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    SUBSCRIBING = "SUBSCRIBING"
    AWAITING_FRESH_DATA = "AWAITING_FRESH_DATA"
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    RECONNECTING = "RECONNECTING"
    DISCONNECTED = "DISCONNECTED"
    FAILED = "FAILED"


class WebSocketMode(StrEnum):
    LTP = "ltp"
    QUOTE = "quote"
    FULL = "full"


class WebSocketTransport(Protocol):
    def connect(self) -> None: ...

    def subscribe(self, tokens: Sequence[int]) -> None: ...

    def set_mode(self, mode: str, tokens: Sequence[int]) -> None: ...

    def close(self) -> None: ...


class KiteTickerClient(Protocol):
    """Narrow surface implemented by the official ``kiteconnect.KiteTicker`` client."""

    def connect(self, *, threaded: bool = False) -> None: ...

    def subscribe(self, instrument_tokens: list[int]) -> object: ...

    def set_mode(self, mode: str, instrument_tokens: list[int]) -> object: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class KiteTickerTransport:
    """Delegate transport operations to an injected official KiteTicker instance."""

    ticker: KiteTickerClient

    def connect(self) -> None:
        self.ticker.connect(threaded=True)

    def subscribe(self, tokens: Sequence[int]) -> None:
        self.ticker.subscribe(list(tokens))

    def set_mode(self, mode: str, tokens: Sequence[int]) -> None:
        self.ticker.set_mode(mode, list(tokens))

    def close(self) -> None:
        self.ticker.close()


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    approved_instruments: dict[InstrumentToken, InstrumentKey]
    mode: WebSocketMode = WebSocketMode.QUOTE
    heartbeat_timeout: timedelta = timedelta(seconds=10)
    maximum_quote_age: timedelta = timedelta(seconds=15)
    reconnect_initial_delay: timedelta = timedelta(seconds=1)
    reconnect_maximum_delay: timedelta = timedelta(seconds=30)
    reconnect_max_attempts: int = 8

    def __post_init__(self) -> None:
        if not self.approved_instruments:
            raise LiveDataError("universe_empty", "Live collector universe cannot be empty")
        if len(set(self.approved_instruments.values())) != len(self.approved_instruments):
            raise LiveDataError("universe_duplicate", "Instrument keys must be unique")
        durations = (
            self.heartbeat_timeout,
            self.maximum_quote_age,
            self.reconnect_initial_delay,
            self.reconnect_maximum_delay,
        )
        if any(item <= timedelta(0) for item in durations) or self.reconnect_max_attempts <= 0:
            raise LiveDataError("collector_config_invalid", "Collector limits must be positive")
        if self.reconnect_initial_delay > self.reconnect_maximum_delay:
            raise LiveDataError(
                "reconnect_config_invalid", "Initial reconnect delay exceeds maximum"
            )


@dataclass(frozen=True, slots=True)
class RawTick:
    instrument_token: InstrumentToken
    exchange_timestamp: datetime
    broker_timestamp: datetime | None
    last_price: Money
    bid: Money | None
    ask: Money | None
    last_quantity: int
    cumulative_volume: int
    broker_sequence: int | None = None

    def __post_init__(self) -> None:
        _aware(self.exchange_timestamp)
        if self.broker_timestamp is not None:
            _aware(self.broker_timestamp)


@dataclass(frozen=True, slots=True)
class LiveTick:
    event_id: UUID
    instrument_token: InstrumentToken
    instrument: InstrumentKey
    exchange_timestamp: datetime
    broker_timestamp: datetime | None
    received_at: datetime
    processed_at: datetime
    last_price: Money
    bid: Money | None
    ask: Money | None
    last_quantity: int
    cumulative_volume: int
    broker_sequence: int | None
    connection_generation: int


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    ordinal: int
    kind: str
    occurred_at: datetime
    received_at: datetime
    processed_at: datetime
    broker_sequence: int | None
    payload_json: str


@dataclass(frozen=True, slots=True)
class RecordingResult:
    session_id: UUID
    parquet_path: Path
    manifest_path: Path
    event_count: int
    checksum_sha256: str


@dataclass(slots=True)
class SessionRecorder:
    root: Path
    session_id: UUID
    started_at: datetime
    provider: str = "zerodha"
    _events: list[RecordedEvent] = field(default_factory=list)
    _closed: bool = False

    def __post_init__(self) -> None:
        _aware(self.started_at)
        if not self.provider.strip():
            raise LiveDataError("provider_invalid", "Recording provider cannot be blank")

    def record(
        self,
        kind: str,
        payload: object,
        *,
        occurred_at: datetime,
        received_at: datetime,
        processed_at: datetime,
        broker_sequence: int | None = None,
    ) -> None:
        if self._closed:
            raise LiveDataError("recording_closed", "Recording session is already closed")
        if not kind.strip():
            raise LiveDataError("event_kind_invalid", "Recorded event kind cannot be blank")
        for value in (occurred_at, received_at, processed_at):
            _aware(value)
        if self._events and received_at < self._events[-1].received_at:
            raise LiveDataError(
                "receive_time_non_monotonic", "Recorded receive time cannot move backwards"
            )
        self._events.append(
            RecordedEvent(
                len(self._events),
                kind,
                occurred_at.astimezone(UTC),
                received_at.astimezone(UTC),
                processed_at.astimezone(UTC),
                broker_sequence,
                json.dumps(payload, default=_json_default, sort_keys=True, separators=(",", ":")),
            )
        )

    def close(self, ended_at: datetime) -> RecordingResult:
        _aware(ended_at)
        if self._closed:
            raise LiveDataError("recording_closed", "Recording session is already closed")
        target = (
            self.root
            / "raw"
            / f"provider={self.provider}"
            / "type=ticks"
            / f"date={self.started_at.date().isoformat()}"
            / f"hour={self.started_at:%H}"
            / f"session={self.session_id}.parquet"
        )
        manifest = target.with_suffix(".manifest.json")
        if target.exists() or manifest.exists():
            raise LiveDataError("recording_exists", "Immutable recording path already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial")
        try:
            rows = [
                {
                    "ordinal": item.ordinal,
                    "kind": item.kind,
                    "occurred_at": item.occurred_at,
                    "received_at": item.received_at,
                    "processed_at": item.processed_at,
                    "broker_sequence": item.broker_sequence,
                    "payload_json": item.payload_json,
                }
                for item in self._events
            ]
            schema = pa.schema(
                [
                    ("ordinal", pa.int64()),
                    ("kind", pa.string()),
                    ("occurred_at", pa.timestamp("us", tz="UTC")),
                    ("received_at", pa.timestamp("us", tz="UTC")),
                    ("processed_at", pa.timestamp("us", tz="UTC")),
                    ("broker_sequence", pa.int64()),
                    ("payload_json", pa.string()),
                ]
            )
            pq.write_table(pa.Table.from_pylist(rows, schema=schema), partial, compression="zstd")
            partial.replace(target)
            checksum = _checksum(target)
            _write_once(
                manifest,
                {
                    "schema_version": 1,
                    "session_id": str(self.session_id),
                    "provider": self.provider,
                    "started_at": self.started_at.astimezone(UTC).isoformat(),
                    "ended_at": ended_at.astimezone(UTC).isoformat(),
                    "event_count": len(rows),
                    "parquet_path": str(target),
                    "checksum_sha256": checksum,
                },
            )
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        self._closed = True
        return RecordingResult(self.session_id, target, manifest, len(self._events), checksum)


@dataclass(frozen=True, slots=True)
class HealthDecision:
    healthy: bool
    state: FeedState
    reasons: tuple[str, ...]


TickHandler = Callable[[LiveTick], None]
OrderUpdateHandler = Callable[[Mapping[str, object]], None]


@dataclass(slots=True)
class LiveDataCollector:
    config: CollectorConfig
    transport: WebSocketTransport
    recorder: SessionRecorder
    clock: Clock
    tick_handler: TickHandler
    order_update_handler: OrderUpdateHandler | None = None
    state: FeedState = FeedState.STOPPED
    connection_generation: int = 0
    reconnect_attempt: int = 0
    next_reconnect_at: datetime | None = None
    _subscribed: set[InstrumentToken] = field(default_factory=set)
    _fresh_generation: dict[InstrumentToken, int] = field(default_factory=dict)
    _latest_exchange: dict[InstrumentToken, datetime] = field(default_factory=dict)
    _latest_received: dict[InstrumentToken, datetime] = field(default_factory=dict)
    _seen: dict[str, str] = field(default_factory=dict)
    _last_message_at: datetime | None = None

    def start(self) -> None:
        if self.state is not FeedState.STOPPED:
            raise LiveDataError("collector_started", "Collector is already running")
        self.state = FeedState.CONNECTING
        self._lifecycle("connect_requested", {})
        self.transport.connect()

    def on_connected(self) -> None:
        if self.state not in {FeedState.CONNECTING, FeedState.RECONNECTING}:
            raise LiveDataError("connect_unexpected", "Unexpected WebSocket connection callback")
        self.connection_generation += 1
        self.state = FeedState.SUBSCRIBING
        tokens = sorted(int(item) for item in self.config.approved_instruments)
        self.transport.subscribe(tokens)
        self.transport.set_mode(self.config.mode.value, tokens)
        self._subscribed = set(self.config.approved_instruments)
        self._fresh_generation.clear()
        self._last_message_at = self.clock.now()
        self.state = FeedState.AWAITING_FRESH_DATA
        self.next_reconnect_at = None
        self._lifecycle(
            "subscribed",
            {
                "tokens": tokens,
                "mode": self.config.mode.value,
                "generation": self.connection_generation,
            },
        )

    def on_ticks(self, ticks: Sequence[RawTick]) -> tuple[LiveTick, ...]:
        now = self.clock.now()
        if self.state not in {
            FeedState.AWAITING_FRESH_DATA,
            FeedState.HEALTHY,
            FeedState.STALE,
        }:
            for raw in ticks:
                self._violation("tick_while_disconnected", raw, now)
            return ()
        self._last_message_at = now
        accepted: list[LiveTick] = []
        for raw in ticks:
            try:
                tick = self._validate(raw, now)
            except LiveDataError as error:
                self._violation(error.code, raw, now)
                continue
            identity = _tick_identity(raw)
            payload_hash = _hash(asdict(raw))
            existing = self._seen.get(identity)
            if existing is not None:
                if existing != payload_hash:
                    self._violation("duplicate_tick_conflict", raw, now)
                continue
            previous = self._latest_exchange.get(raw.instrument_token)
            if previous is not None and raw.exchange_timestamp < previous:
                self._violation("tick_out_of_order", raw, now)
                continue
            self._seen[identity] = payload_hash
            self._latest_exchange[raw.instrument_token] = raw.exchange_timestamp
            self._latest_received[raw.instrument_token] = now
            self._fresh_generation[raw.instrument_token] = self.connection_generation
            self._record_tick(tick)
            self.tick_handler(tick)
            accepted.append(tick)
        if self._all_fresh(now):
            if self.state is FeedState.AWAITING_FRESH_DATA and self.connection_generation > 1:
                self._lifecycle("data_gap_ended", {"generation": self.connection_generation})
            self.state = FeedState.HEALTHY
            self.reconnect_attempt = 0
        elif self.state is FeedState.HEALTHY:
            self.state = FeedState.STALE
        return tuple(accepted)

    def on_order_update(self, payload: Mapping[str, object]) -> None:
        now = self.clock.now()
        self.recorder.record(
            "order_update",
            dict(payload),
            occurred_at=now,
            received_at=now,
            processed_at=now,
        )
        if self.order_update_handler is not None:
            self.order_update_handler(payload)

    def on_disconnected(self, code: int, reason: str) -> None:
        now = self.clock.now()
        self._lifecycle("disconnected", {"code": code, "reason": reason})
        self._lifecycle("data_gap_started", {"generation": self.connection_generation})
        self._subscribed.clear()
        self._fresh_generation.clear()
        self.state = FeedState.RECONNECTING
        self.reconnect_attempt += 1
        if self.reconnect_attempt > self.config.reconnect_max_attempts:
            self.state = FeedState.FAILED
            self.next_reconnect_at = None
            return
        exponent = self.reconnect_attempt - 1
        delay_seconds = min(
            self.config.reconnect_initial_delay.total_seconds() * (2**exponent),
            self.config.reconnect_maximum_delay.total_seconds(),
        )
        self.next_reconnect_at = now + timedelta(seconds=delay_seconds)

    def poll(self) -> FeedState:
        now = self.clock.now()
        if (
            self.state is FeedState.RECONNECTING
            and self.next_reconnect_at is not None
            and now >= self.next_reconnect_at
        ):
            self.transport.connect()
            self.next_reconnect_at = None
        if (
            self.state in {FeedState.HEALTHY, FeedState.AWAITING_FRESH_DATA}
            and self._last_message_at is not None
            and now - self._last_message_at > self.config.heartbeat_timeout
        ):
            self.state = FeedState.STALE
            self._lifecycle("heartbeat_missing", {})
        elif self.state is FeedState.HEALTHY and not self._all_fresh(now):
            self.state = FeedState.STALE
            self._lifecycle("quote_stale", {})
        return self.state

    def health(self) -> HealthDecision:
        self.poll()
        reasons: list[str] = []
        expected = set(self.config.approved_instruments)
        if self.state is not FeedState.HEALTHY:
            reasons.append(f"feed_{self.state.value.lower()}")
        if self._subscribed != expected:
            reasons.append("subscriptions_incomplete")
        if not self._all_fresh(self.clock.now()):
            reasons.append("fresh_quotes_incomplete")
        return HealthDecision(not reasons, self.state, tuple(reasons))

    def stop(self) -> RecordingResult:
        if self.state is FeedState.STOPPED:
            raise LiveDataError("collector_stopped", "Collector is already stopped")
        self.transport.close()
        now = self.clock.now()
        self._lifecycle("collector_stopped", {})
        self.state = FeedState.STOPPED
        return self.recorder.close(now)

    def _validate(self, raw: RawTick, now: datetime) -> LiveTick:
        instrument = self.config.approved_instruments.get(raw.instrument_token)
        if instrument is None:
            raise LiveDataError("instrument_unknown", "Tick instrument is not approved")
        if raw.last_price.amount <= 0 or raw.last_quantity < 0 or raw.cumulative_volume < 0:
            raise LiveDataError("tick_numeric_invalid", "Tick fields are invalid")
        if raw.bid is not None and raw.ask is not None and raw.bid.amount > raw.ask.amount:
            raise LiveDataError("tick_crossed_market", "Tick bid exceeds ask")
        if now - raw.exchange_timestamp > self.config.maximum_quote_age:
            raise LiveDataError("tick_stale", "Tick exceeds maximum quote age")
        if raw.exchange_timestamp > now:
            raise LiveDataError("tick_from_future", "Tick exchange time is in the future")
        event_id = uuid5(EVENT_NAMESPACE, _tick_identity(raw))
        return LiveTick(
            event_id,
            raw.instrument_token,
            instrument,
            raw.exchange_timestamp.astimezone(UTC),
            raw.broker_timestamp.astimezone(UTC) if raw.broker_timestamp else None,
            now.astimezone(UTC),
            now.astimezone(UTC),
            raw.last_price,
            raw.bid,
            raw.ask,
            raw.last_quantity,
            raw.cumulative_volume,
            raw.broker_sequence,
            self.connection_generation,
        )

    def _all_fresh(self, now: datetime) -> bool:
        return bool(self._subscribed) and all(
            self._fresh_generation.get(token) == self.connection_generation
            and token in self._latest_received
            and now - self._latest_received[token] <= self.config.maximum_quote_age
            for token in self.config.approved_instruments
        )

    def _record_tick(self, tick: LiveTick) -> None:
        self.recorder.record(
            "tick",
            asdict(tick),
            occurred_at=tick.exchange_timestamp,
            received_at=tick.received_at,
            processed_at=tick.processed_at,
            broker_sequence=tick.broker_sequence,
        )

    def _lifecycle(self, kind: str, payload: object) -> None:
        now = self.clock.now()
        self.recorder.record(kind, payload, occurred_at=now, received_at=now, processed_at=now)

    def _violation(self, code: str, raw: RawTick, now: datetime) -> None:
        self.recorder.record(
            "data_quality_violation",
            {"rule": code, "action": "quarantined", "sample": asdict(raw)},
            occurred_at=raw.exchange_timestamp,
            received_at=now,
            processed_at=now,
            broker_sequence=raw.broker_sequence,
        )


ReplayHandler = Callable[[RecordedEvent], None]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    session_id: UUID
    event_count: int
    stream_hash: str
    final_clock: datetime


@dataclass(slots=True)
class ReplayEngine:
    clock: SimulatedClock

    def replay(
        self,
        manifest_path: Path,
        handler: ReplayHandler,
        *,
        speed: Decimal = Decimal(1),
    ) -> ReplayResult:
        if speed <= 0:
            raise LiveDataError("replay_speed_invalid", "Replay speed must be positive")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            parquet_path = Path(str(manifest["parquet_path"]))
            if _checksum(parquet_path) != manifest["checksum_sha256"]:
                raise LiveDataError("recording_checksum", "Recording checksum validation failed")
            table = pq.read_table(parquet_path)
        except LiveDataError:
            raise
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
            raise LiveDataError("recording_invalid", "Replay recording is invalid") from error
        rows = sorted(table.to_pylist(), key=lambda item: int(item["ordinal"]))
        if len(rows) != int(manifest["event_count"]):
            raise LiveDataError("recording_count", "Recording row count does not match manifest")
        stream = hashlib.sha256()
        if rows:
            first_received = _utc(rows[0]["received_at"])
            virtual_start = self.clock.now()
            for raw in rows:
                received = _utc(raw["received_at"])
                elapsed_microseconds = Decimal(
                    str((received - first_received).total_seconds() * 1_000_000)
                )
                virtual = virtual_start + timedelta(microseconds=int(elapsed_microseconds / speed))
                self.clock.set(virtual)
                event = RecordedEvent(
                    int(raw["ordinal"]),
                    str(raw["kind"]),
                    _utc(raw["occurred_at"]),
                    received,
                    _utc(raw["processed_at"]),
                    int(raw["broker_sequence"]) if raw["broker_sequence"] is not None else None,
                    str(raw["payload_json"]),
                )
                canonical = json.dumps(asdict(event), default=_json_default, sort_keys=True)
                stream.update(canonical.encode())
                handler(event)
        return ReplayResult(
            UUID(str(manifest["session_id"])),
            len(rows),
            stream.hexdigest(),
            self.clock.now(),
        )


def new_recorder(root: Path, clock: Clock) -> SessionRecorder:
    return SessionRecorder(root, uuid4(), clock.now())


def _tick_identity(tick: RawTick) -> str:
    discriminator = (
        str(tick.broker_sequence)
        if tick.broker_sequence is not None
        else f"payload:{_hash(asdict(tick))}"
    )
    return "|".join(
        (
            str(int(tick.instrument_token)),
            tick.exchange_timestamp.astimezone(UTC).isoformat(),
            discriminator,
        )
    )


def _hash(value: object) -> str:
    raw = json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, Money):
        return {"amount": str(value.amount), "currency": value.currency.value}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (UUID, StrEnum, Decimal)):
        return str(value)
    return value


def _write_once(path: Path, value: object) -> None:
    if path.exists():
        raise LiveDataError("immutable_path_exists", "Immutable metadata path already exists")
    partial = path.with_suffix(".partial")
    partial.write_text(
        json.dumps(value, default=_json_default, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    partial.replace(path)


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LiveDataError("time_naive", "Live-data timestamps must be timezone-aware")


def _utc(value: datetime) -> datetime:
    _aware(value)
    return value.astimezone(UTC)

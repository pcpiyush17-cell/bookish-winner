from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from personal_quant.clocks import SimulatedClock
from personal_quant.domain.identifiers import InstrumentKey, InstrumentToken
from personal_quant.domain.money import Money
from personal_quant.live_data import (
    CollectorConfig,
    FeedState,
    KiteTickerTransport,
    LiveDataCollector,
    LiveDataError,
    LiveTick,
    RawTick,
    RecordedEvent,
    ReplayEngine,
    SessionRecorder,
    WebSocketMode,
)

NOW = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)
TOKEN_ONE = InstrumentToken(101)
TOKEN_TWO = InstrumentToken(202)
INSTRUMENT_ONE = InstrumentKey("NSE:INFY")
INSTRUMENT_TWO = InstrumentKey("NSE:TCS")


class MockTransport:
    def __init__(self) -> None:
        self.connects = 0
        self.subscriptions: list[tuple[int, ...]] = []
        self.modes: list[tuple[str, tuple[int, ...]]] = []
        self.closed = False

    def connect(self) -> None:
        self.connects += 1

    def subscribe(self, tokens: Sequence[int]) -> None:
        self.subscriptions.append(tuple(tokens))

    def set_mode(self, mode: str, tokens: Sequence[int]) -> None:
        self.modes.append((mode, tuple(tokens)))

    def close(self) -> None:
        self.closed = True


class MockKiteTicker:
    def __init__(self) -> None:
        self.threaded = False
        self.tokens: list[int] = []
        self.mode = ""
        self.closed = False

    def connect(self, *, threaded: bool = False) -> None:
        self.threaded = threaded

    def subscribe(self, instrument_tokens: list[int]) -> None:
        self.tokens = instrument_tokens

    def set_mode(self, mode: str, instrument_tokens: list[int]) -> None:
        self.mode = mode
        self.tokens = instrument_tokens

    def close(self) -> None:
        self.closed = True


def raw_tick(
    token: InstrumentToken,
    at: datetime,
    *,
    sequence: int | None,
    price: str = "100",
) -> RawTick:
    return RawTick(
        token,
        at,
        at,
        Money.from_value(price),
        Money.from_value(Decimal(price) - Decimal("0.05")),
        Money.from_value(Decimal(price) + Decimal("0.05")),
        5,
        1000,
        sequence,
    )


def collector(
    tmp_path: Path,
) -> tuple[
    SimulatedClock,
    MockTransport,
    list[LiveTick],
    list[dict[str, object]],
    LiveDataCollector,
]:
    clock = SimulatedClock(NOW)
    transport = MockTransport()
    received: list[LiveTick] = []
    orders: list[dict[str, object]] = []
    recorder = SessionRecorder(tmp_path, UUID_ONE, NOW)
    value = LiveDataCollector(
        CollectorConfig(
            {TOKEN_ONE: INSTRUMENT_ONE, TOKEN_TWO: INSTRUMENT_TWO},
            mode=WebSocketMode.QUOTE,
            heartbeat_timeout=timedelta(seconds=5),
            maximum_quote_age=timedelta(seconds=10),
            reconnect_initial_delay=timedelta(seconds=1),
            reconnect_maximum_delay=timedelta(seconds=4),
            reconnect_max_attempts=3,
        ),
        transport,
        recorder,
        clock,
        received.append,
        lambda payload: orders.append(dict(payload)),
    )
    return clock, transport, received, orders, value


UUID_ONE = UUID("d14fe9e5-b5cc-45cb-b0ae-2bcc9658c222")


def make_healthy(value: LiveDataCollector) -> None:
    value.start()
    value.on_connected()
    value.on_ticks((raw_tick(TOKEN_ONE, NOW, sequence=1), raw_tick(TOKEN_TWO, NOW, sequence=1)))


def assert_state(value: LiveDataCollector, expected: FeedState) -> None:
    assert value.state is expected


def test_connection_requires_subscription_and_fresh_universe(tmp_path: Path) -> None:
    _, transport, received, _, value = collector(tmp_path)
    value.start()
    assert_state(value, FeedState.CONNECTING)
    assert not value.health().healthy
    value.on_connected()
    assert_state(value, FeedState.AWAITING_FRESH_DATA)
    assert transport.subscriptions == [(101, 202)]
    assert transport.modes == [("quote", (101, 202))]

    value.on_ticks((raw_tick(TOKEN_ONE, NOW, sequence=1),))
    assert_state(value, FeedState.AWAITING_FRESH_DATA)
    value.on_ticks((raw_tick(TOKEN_TWO, NOW, sequence=1),))
    assert value.health().healthy
    assert_state(value, FeedState.HEALTHY)
    assert len(received) == 2


def test_reconnect_blocks_until_resubscribed_and_every_quote_is_fresh(tmp_path: Path) -> None:
    clock, transport, _, _, value = collector(tmp_path)
    make_healthy(value)
    value.on_disconnected(1006, "network lost")
    assert_state(value, FeedState.RECONNECTING)
    assert value.on_ticks((raw_tick(TOKEN_ONE, NOW, sequence=99),)) == ()
    assert not value.health().healthy
    clock.advance(timedelta(seconds=1))
    value.poll()
    assert transport.connects == 2
    value.on_connected()
    assert_state(value, FeedState.AWAITING_FRESH_DATA)
    at = clock.now()
    value.on_ticks((raw_tick(TOKEN_ONE, at, sequence=2),))
    assert not value.health().healthy
    value.on_ticks((raw_tick(TOKEN_TWO, at, sequence=2),))
    assert value.health().healthy
    assert transport.subscriptions == [(101, 202), (101, 202)]


def test_official_client_transport_delegates_without_owning_credentials() -> None:
    ticker = MockKiteTicker()
    transport = KiteTickerTransport(ticker)
    transport.connect()
    transport.subscribe((101, 202))
    transport.set_mode("quote", (101, 202))
    transport.close()
    assert ticker.threaded
    assert ticker.tokens == [101, 202]
    assert ticker.mode == "quote"
    assert ticker.closed


def test_heartbeat_staleness_duplicates_conflicts_and_ordering_fail_closed(
    tmp_path: Path,
) -> None:
    clock, _, received, orders, value = collector(tmp_path)
    make_healthy(value)
    duplicate = raw_tick(TOKEN_ONE, NOW, sequence=1)
    assert value.on_ticks((duplicate,)) == ()
    conflict = raw_tick(TOKEN_ONE, NOW, sequence=1, price="101")
    assert value.on_ticks((conflict,)) == ()
    older = raw_tick(TOKEN_ONE, NOW - timedelta(seconds=1), sequence=2)
    assert value.on_ticks((older,)) == ()
    assert len(received) == 2
    value.on_order_update({"order_id": "sandbox-1", "status": "OPEN"})
    assert orders == [{"order_id": "sandbox-1", "status": "OPEN"}]

    clock.advance(timedelta(seconds=6))
    assert value.poll() is FeedState.STALE
    decision = value.health()
    assert not decision.healthy
    assert "feed_stale" in decision.reasons


def test_same_second_kite_ticks_without_sequence_use_payload_identity(tmp_path: Path) -> None:
    _, _, received, _, value = collector(tmp_path)
    make_healthy(value)
    first = raw_tick(TOKEN_ONE, NOW, sequence=None, price="101")
    changed = raw_tick(TOKEN_ONE, NOW, sequence=None, price="102")

    accepted_first = value.on_ticks((first,))
    accepted_changed = value.on_ticks((changed,))
    exact_retransmission = value.on_ticks((first,))

    assert len(accepted_first) == len(accepted_changed) == 1
    assert accepted_first[0].event_id != accepted_changed[0].event_id
    assert exact_retransmission == ()
    assert [tick.last_price for tick in received[-2:]] == [
        Money.from_value("101"),
        Money.from_value("102"),
    ]


def test_recorded_session_replays_deterministically_at_any_speed(tmp_path: Path) -> None:
    clock, transport, _, _, value = collector(tmp_path)
    make_healthy(value)
    clock.advance(timedelta(seconds=2))
    value.on_ticks(
        (
            raw_tick(TOKEN_ONE, clock.now(), sequence=2, price="101"),
            raw_tick(TOKEN_TWO, clock.now(), sequence=2, price="102"),
        )
    )
    recording = value.stop()
    assert transport.closed
    assert recording.event_count > 4
    assert recording.parquet_path.is_file()
    assert recording.manifest_path.is_file()

    first_events: list[RecordedEvent] = []
    second_events: list[RecordedEvent] = []
    first = ReplayEngine(SimulatedClock(NOW)).replay(recording.manifest_path, first_events.append)
    second = ReplayEngine(SimulatedClock(NOW)).replay(
        recording.manifest_path, second_events.append, speed=Decimal("10")
    )
    assert first.session_id == second.session_id == UUID_ONE
    assert first.event_count == second.event_count == recording.event_count
    assert first.stream_hash == second.stream_hash
    assert first_events == second_events
    assert first.final_clock > second.final_clock


def test_malformed_and_tampered_data_are_rejected(tmp_path: Path) -> None:
    _, _, received, _, value = collector(tmp_path)
    value.start()
    value.on_connected()
    unknown = raw_tick(InstrumentToken(999), NOW, sequence=1)
    crossed = RawTick(
        TOKEN_ONE,
        NOW,
        NOW,
        Money.from_value("100"),
        Money.from_value("101"),
        Money.from_value("100"),
        1,
        1,
        3,
    )
    assert value.on_ticks((unknown, crossed)) == ()
    assert received == []
    recording = value.stop()
    recording.parquet_path.write_bytes(recording.parquet_path.read_bytes() + b"tampered")
    with pytest.raises(LiveDataError) as error:
        ReplayEngine(SimulatedClock(NOW)).replay(recording.manifest_path, lambda event: None)
    assert error.value.code == "recording_checksum"

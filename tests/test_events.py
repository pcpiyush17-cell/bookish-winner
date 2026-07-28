from datetime import UTC, datetime
from uuid import uuid4

import pytest

from personal_quant.domain.events import CoreEventType, EventMeta
from personal_quant.domain.identifiers import RunId


def event_meta(**overrides: object) -> EventMeta:
    values: dict[str, object] = {
        "event_id": uuid4(),
        "event_type": CoreEventType.SIGNAL_CREATED,
        "schema_version": 1,
        "occurred_at": datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        "received_at": datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        "source": "unit-test",
        "correlation_id": None,
        "causation_id": None,
        "run_id": RunId(uuid4()),
    }
    values.update(overrides)
    return EventMeta(**values)  # type: ignore[arg-type]


def test_event_metadata_is_immutable() -> None:
    meta = event_meta()

    assert meta.event_type.value == "SignalCreated"
    with pytest.raises(AttributeError):
        meta.source = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["occurred_at", "received_at"])
def test_event_metadata_requires_aware_timestamps(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must be timezone-aware"):
        event_meta(**{field: datetime(2026, 7, 28, 10, 0)})


def test_event_metadata_validates_schema_and_source() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        event_meta(schema_version=0)
    with pytest.raises(ValueError, match="source"):
        event_meta(source="  ")


def test_all_blueprint_core_event_names_are_unique() -> None:
    names = [event_type.value for event_type in CoreEventType]

    assert len(names) == 25
    assert len(names) == len(set(names))

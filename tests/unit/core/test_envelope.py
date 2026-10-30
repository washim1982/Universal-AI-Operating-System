import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from aios.core.envelope import DataClass, Event
from aios.core.ids import is_ulid, new_ulid
from aios.core.topics import EventKind
from aios.testing import make_event

CLOUDEVENTS_CORE = {
    "specversion",
    "id",
    "source",
    "type",
    "subject",
    "time",
    "datacontenttype",
    "dataschema",
    "data",
}


def test_defaults() -> None:
    event = make_event()
    assert event.specversion == "1.0"
    assert is_ulid(event.id)
    assert event.time.tzinfo is UTC
    assert event.hopcount == 0
    assert event.dataclass is DataClass.INTERNAL
    assert event.kind is EventKind.FACT


def test_extension_attribute_names_follow_cloudevents() -> None:
    extensions = set(Event.model_fields) - CLOUDEVENTS_CORE
    assert extensions  # sanity
    for name in extensions:
        assert re.fullmatch(r"[a-z0-9]{1,20}", name), name


def test_json_round_trip() -> None:
    event = make_event("research.plan.created", {"tasks": [1, 2]}, run_id=new_ulid())
    encoded = event.model_dump_json()
    assert json.loads(encoded)["time"].endswith("Z")
    assert Event.model_validate_json(encoded) == event


def test_matches_the_requirements_example() -> None:
    example = {
        "specversion": "1.0",
        "id": "01JD7X2K9Q5V8M3N4P6R7S8T9V",
        "source": "aios://acme/apps/ai-researcher/agents/research-agent",
        "type": "task.completed",
        "subject": "run/01JD7X1C3BWQ8R2N5M4K6P7S9T",
        "time": "2026-10-28T10:15:30.123Z",
        "datacontenttype": "application/json",
        "dataschema": "aios://schemas/task.completed/1",
        "tenantid": "acme",
        "appid": "ai-researcher",
        "runid": "01JD7X1C3BWQ8R2N5M4K6P7S9T",
        "correlationid": "01JD7X1C3BWQ8R2N5M4K6P7S9T",
        "causationid": "01JD7X2A4H6J8K0M2N4P6Q8R0S",
        "hopcount": 4,
        "principal": "agent:research-agent@acme",
        "onbehalfof": "user:alice@acme",
        "dataclass": "internal",
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "data": {"capability": "research.web", "output": {"summary": "…", "sources": ["…"]}},
    }
    event = Event.model_validate(example)
    assert event.model_dump(mode="json", exclude_none=True)["time"] == "2026-10-28T10:15:30.123000Z"


def test_times_are_normalised_to_utc() -> None:
    plus_two = datetime(2026, 10, 28, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert make_event(time=plus_two).time == datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def test_naive_times_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_event(time=datetime(2026, 10, 28, 12, 0))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenantid", "Acme"),
        ("tenantid", "acme.corp"),
        ("appid", ""),
        ("principal", "alice"),
        ("principal", "robot:alice@acme"),
        ("traceparent", "01-xyz"),
        ("hopcount", -1),
        ("id", "not-a-ulid"),
        ("specversion", "0.3"),
        ("source", "has space"),
        ("type", "Task.Completed"),
        ("unknown", "x"),
    ],
)
def test_rejects_invalid_attributes(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        make_event(**{field: value})


def test_events_are_immutable() -> None:
    event = make_event()
    with pytest.raises(ValidationError):
        event.hopcount = 5  # type: ignore[misc]


def test_derive_keeps_context_and_tracks_causation() -> None:
    parent = make_event(
        "task.requested",
        {"capability": "research.web"},
        run_id=new_ulid(),
        onbehalfof="user:alice@acme",
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        dataclass=DataClass.CONFIDENTIAL,
        hopcount=3,
    )
    child = parent.derive(
        "task.completed",
        {"capability": "research.web"},
        source="aios://acme/apps/ai-researcher/agents/research-agent",
        principal="agent:research-agent@acme",
    )
    assert child.id != parent.id
    assert child.causationid == parent.id
    assert child.hopcount == 4
    for name in ("tenantid", "appid", "runid", "correlationid", "onbehalfof", "traceparent"):
        assert getattr(child, name) == getattr(parent, name)
    assert child.principal == "agent:research-agent@acme"
    assert child.dataclass is DataClass.CONFIDENTIAL  # never downgraded


def test_derive_can_raise_the_data_class() -> None:
    child = make_event().derive(
        "pii.record.found",
        source="aios://acme/x",
        principal="agent:dlp@acme",
        dataclass=DataClass.RESTRICTED,
    )
    assert child.dataclass is DataClass.RESTRICTED


def test_data_class_ordering() -> None:
    assert [c.rank for c in DataClass] == [0, 1, 2, 3]
    assert DataClass.strictest(DataClass.PUBLIC, DataClass.CONFIDENTIAL) is DataClass.CONFIDENTIAL

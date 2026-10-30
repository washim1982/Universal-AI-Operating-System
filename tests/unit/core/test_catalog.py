from aios.core import catalog
from aios.core.topics import RESERVED_DOMAINS, EventKind


def test_types_are_unique() -> None:
    types = [spec.type for spec in catalog.STANDARD_EVENTS]
    assert len(types) == len(set(types))


def test_standard_events_use_reserved_domains() -> None:
    assert {spec.domain for spec in catalog.STANDARD_EVENTS} <= RESERVED_DOMAINS


def test_every_reserved_domain_has_events() -> None:
    assert {spec.domain for spec in catalog.STANDARD_EVENTS} == RESERVED_DOMAINS


def test_commands_are_the_requested_events() -> None:
    commands = {s.type for s in catalog.STANDARD_EVENTS if s.kind is EventKind.COMMAND}
    assert commands == {
        "task.requested",
        "tool.call.requested",
        "approval.requested",
        "run.cancel.requested",
    }


def test_route_by_names_a_payload_field() -> None:
    for spec in catalog.STANDARD_EVENTS:
        if spec.route_by is not None:
            assert spec.payload is not None
            assert spec.route_by in spec.payload.model_fields


def test_lookup() -> None:
    spec = catalog.get("task.requested")
    assert spec is not None
    assert spec.schema_id == "aios://schemas/task.requested/1"
    assert catalog.get("research.plan.created") is None

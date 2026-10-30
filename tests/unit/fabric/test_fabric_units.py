import pytest
from hypothesis import given
from hypothesis import strategies as st

from aios.core.errors import InvalidEventError
from aios.fabric import InMemoryEventBus, RetryPolicy, Scope
from aios.fabric.subjects import physical_pattern, physical_subject, routing_subject
from aios.testing import make_event, task_request


def test_physical_subject_prefixes_tenant_and_app() -> None:
    assert physical_subject(make_event("research.plan.created")) == (
        "acme.ai-researcher.research.plan.created"
    )


def test_task_requests_carry_the_capability_as_routing_suffix() -> None:
    assert routing_subject(task_request("research.web")) == "task.requested.research.web"


@pytest.mark.parametrize("data", [None, {}, {"capability": 3}, {"capability": "Bad Name"}])
def test_task_requests_need_a_valid_capability(data: object) -> None:
    with pytest.raises(InvalidEventError):
        routing_subject(make_event("task.requested", data))


def test_physical_pattern() -> None:
    assert physical_pattern("acme", "*", "task.>") == "acme.*.task.>"


def test_scope_validation() -> None:
    assert Scope.platform() == Scope("*", "*")
    with pytest.raises(ValueError, match="tenant"):
        Scope("Not Valid")
    with pytest.raises(ValueError, match="app"):
        Scope("acme", "a.b")


def test_scope_contains() -> None:
    event = make_event(tenant="acme", app="desk")
    assert Scope("acme").contains(event)
    assert Scope("acme", "desk").contains(event)
    assert not Scope("acme", "other").contains(event)
    assert not Scope("globex").contains(event)


def test_backoff_without_jitter_grows_and_caps() -> None:
    policy = RetryPolicy(initial_backoff=1, multiplier=2, max_backoff=5, jitter=False)
    assert [policy.backoff(n) for n in range(1, 6)] == [1, 2, 4, 5, 5]


def test_backoff_survives_huge_attempt_numbers() -> None:
    policy = RetryPolicy(max_deliveries=10_000, max_backoff=60, jitter=False)
    assert policy.backoff(5_000) == 60


async def test_request_without_reply_types_publishes_nothing() -> None:
    async with InMemoryEventBus() as bus:
        with pytest.raises(ValueError, match="reply type"):
            await bus.request(task_request("research.web"), reply_types=[])
        assert [s async for s in bus.replay(Scope.platform())] == []


@given(st.integers(min_value=1, max_value=50))
def test_backoff_jitter_stays_within_half_and_full_delay(attempt: int) -> None:
    policy = RetryPolicy(initial_backoff=0.5, max_backoff=60)
    capped = min(60, 0.5 * 2 ** (attempt - 1))
    assert capped / 2 <= policy.backoff(attempt) <= capped

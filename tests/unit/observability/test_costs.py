"""Costs by App, agent, model and person (FR-OBS-06)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from aios.core.envelope import Event
from aios.observability.costs import costs_of, month_after, month_start


def call(
    cost: float,
    *,
    app: str = "desk",
    agent: str = "a",
    model: str = "m",
    user: str | None = "user:lee@acme",
    when: datetime | None = None,
) -> Event:
    return Event(
        source="aios://acme/apps/x",
        type="model.call.completed",
        tenantid="acme",
        appid=app,
        principal=f"agent:{agent}@acme",
        onbehalfof=user,
        time=when or datetime(2026, 11, 2, tzinfo=UTC),
        data={
            "agent": agent,
            "provider": "p",
            "model": model,
            "usage": {"cost_usd": cost, "input_tokens": 10, "output_tokens": 5},
        },
    )


def alert(type: str, **data: Any) -> Event:
    return Event(
        source="aios://acme/apps/x",
        type=type,
        tenantid="acme",
        appid="desk",
        principal="system:models",
        time=datetime(2026, 11, 2, tzinfo=UTC),
        data=data,
    )


def test_spending_is_added_up_each_way() -> None:
    found = costs_of(
        [call(1.0), call(2.0, app="other", agent="b", model="n", user=None)], "2026-11"
    )
    assert (found.cost_usd, found.calls) == (3.0, 2)
    assert [(s.key, s.cost_usd) for s in found.by_app] == [("other", 2.0), ("desk", 1.0)]
    assert [s.key for s in found.by_model] == ["p/n", "p/m"]
    assert [s.key for s in found.by_user] == ["(nobody)", "user:lee@acme"]
    assert found.by_agent[0].input_tokens == 10


def test_only_the_month_counts() -> None:
    october = call(5.0, when=datetime(2026, 10, 31, 23, 59, tzinfo=UTC))
    december = call(7.0, when=datetime(2026, 12, 1, tzinfo=UTC))
    assert costs_of([october, call(1.0), december], "2026-11").cost_usd == 1.0


def test_december_ends_with_the_year() -> None:
    assert month_after(month_start("2026-12")) == datetime(2027, 1, 1, tzinfo=UTC)


def test_alerts_are_listed() -> None:
    found = costs_of(
        [
            alert("budget.warning", level="tenant", limit=1.0, observed=0.8),
            alert("budget.exceeded", budget="cost", level="run", limit=2.0, observed=2.1),
        ],
        "2026-11",
    )
    assert [(a.type, a.level, a.limit, a.observed) for a in found.alerts] == [
        ("budget.warning", "tenant", 1.0, 0.8),
        ("budget.exceeded", "run", 2.0, 2.1),
    ]


def test_figures_are_kept_to_eight_places() -> None:
    found = costs_of([call(0.1), call(0.2)], "2026-11")
    assert found.cost_usd == pytest.approx(0.3)
    assert str(found.by_app[0].cost_usd) == "0.3"

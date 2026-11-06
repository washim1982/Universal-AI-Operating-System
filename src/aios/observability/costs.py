"""What a tenant spent in a month, by App, agent, model and person (FR-OBS-06, FR-UI-05).

The figures come from ``model.call.completed``, the same events the ledger was charged with, so
they add up to what budgets counted. Budget alerts are the ``budget.warning`` and
``budget.exceeded`` events of the same month.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from aios.core.envelope import Event

ALERTS = ("budget.warning", "budget.exceeded")


class Spend(BaseModel):
    key: str
    cost_usd: float = 0.0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class Alert(BaseModel):
    type: str
    app: str
    budget: str = Field(default="cost", description="cost, tokens, steps, hops, time or events")
    level: str | None = None
    limit: float | None = None
    observed: float | None = None
    run: str | None = None
    at: datetime


class Costs(BaseModel):
    month: str
    cost_usd: float = 0.0
    calls: int = 0
    by_app: list[Spend] = Field(default_factory=list[Spend])
    by_agent: list[Spend] = Field(default_factory=list[Spend])
    by_model: list[Spend] = Field(default_factory=list[Spend])
    by_user: list[Spend] = Field(default_factory=list[Spend])
    alerts: list[Alert] = Field(default_factory=list[Alert])


def month_start(month: str) -> datetime:
    """The first moment of ``YYYY-MM``, in UTC."""
    return datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)


def month_after(start: datetime) -> datetime:
    return (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )


def _ranked(totals: dict[str, Spend]) -> list[Spend]:
    for spend in totals.values():
        spend.cost_usd = round(spend.cost_usd, 8)
    return sorted(totals.values(), key=lambda spend: (-spend.cost_usd, spend.key))


def costs_of(events: Iterable[Event], month: str) -> Costs:
    """Add up the model calls and alerts among ``events`` that fall in ``month``."""
    start = month_start(month)
    end = month_after(start)
    groups: dict[str, dict[str, Spend]] = {name: {} for name in ("app", "agent", "model", "user")}
    found = Costs(month=month)
    for event in events:
        if not start <= event.time < end:
            continue
        data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
        if event.type in ALERTS:
            found.alerts.append(
                Alert(
                    type=event.type,
                    app=event.appid,
                    budget=str(data.get("budget", "cost")),
                    level=data.get("level"),
                    limit=data.get("limit"),
                    observed=data.get("observed"),
                    run=event.runid,
                    at=event.time,
                )
            )
            continue
        if event.type != "model.call.completed":
            continue
        usage: dict[str, Any] = data.get("usage") or {}
        cost = float(usage.get("cost_usd", 0.0))
        keys = {
            "app": event.appid,
            "agent": str(data.get("agent", "?")),
            "model": f"{data.get('provider')}/{data.get('model')}",
            "user": event.onbehalfof or "(nobody)",
        }
        for name, key in keys.items():
            spend = groups[name].setdefault(key, Spend(key=key))
            spend.cost_usd += cost
            spend.calls += 1
            spend.input_tokens += int(usage.get("input_tokens", 0))
            spend.output_tokens += int(usage.get("output_tokens", 0))
        found.cost_usd += cost
        found.calls += 1
    found.cost_usd = round(found.cost_usd, 8)
    found.by_app = _ranked(groups["app"])
    found.by_agent = _ranked(groups["agent"])
    found.by_model = _ranked(groups["model"])
    found.by_user = _ranked(groups["user"])
    found.alerts.sort(key=lambda alert: alert.at, reverse=True)
    return found

"""Prometheus metrics derived from the event log (WP 1.6; FR-OBS-05, 08).

Run outcomes and durations, task durations, model tokens and cost, tool calls, approvals and how
long people took, dead letters and budget stops. Like the tracer, it is a broadcast subscriber;
the API serves ``/metrics``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from aios.core.envelope import Event
from aios.core.errors import BusClosedError
from aios.fabric.port import EventBus, Scope, Subscription

_DURATIONS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900)
_APPROVAL_WAITS = (60, 300, 900, 3600, 14400, 86400, 259200)
_APPROVALS_ENDED = ("approval.granted", "approval.denied", "approval.expired", "approval.cancelled")


class EventMetrics:
    def __init__(self, bus: EventBus, *, scope: Scope | None = None) -> None:
        self.bus = bus
        self.scope = scope or Scope.platform()
        self.registry = CollectorRegistry()
        labels = ["tenant", "app"]
        self.runs = Counter(
            "aios_runs", "Finished runs", [*labels, "workflow", "outcome"], registry=self.registry
        )
        self.run_seconds = Histogram(
            "aios_run_duration_seconds", "Run duration", [*labels, "workflow"],
            buckets=_DURATIONS, registry=self.registry,
        )  # fmt: skip
        self.task_seconds = Histogram(
            "aios_task_duration_seconds", "Agent task duration", [*labels, "capability", "outcome"],
            buckets=_DURATIONS, registry=self.registry,
        )  # fmt: skip
        self.tokens = Counter(
            "aios_model_tokens", "Model tokens", [*labels, "provider", "model", "direction"],
            registry=self.registry,
        )  # fmt: skip
        self.cost = Counter(
            "aios_model_cost_usd", "Model cost in USD", [*labels, "provider", "model"],
            registry=self.registry,
        )  # fmt: skip
        self.tool_calls = Counter(
            "aios_tool_calls", "Tool calls", [*labels, "tool", "outcome"], registry=self.registry
        )
        self.dead_letters = Counter(
            "aios_dead_letters", "Dead-lettered events", [*labels, "group"], registry=self.registry
        )
        self.cache_tokens = Counter(
            "aios_model_cache_tokens", "Prompt tokens read from or written to a provider's cache",
            [*labels, "provider", "model", "direction"], registry=self.registry,
        )  # fmt: skip
        self.downgrades = Counter(
            "aios_model_downgrades", "Calls a soft budget limit moved to another route",
            [*labels, "provider", "model"], registry=self.registry,
        )  # fmt: skip
        self.budget_stops = Counter(
            "aios_budget_exceeded", "Budget stops", [*labels, "budget", "level"],
            registry=self.registry,
        )  # fmt: skip
        self.budget_warnings = Counter(
            "aios_budget_warnings", "Soft budget limits reached", [*labels, "level"],
            registry=self.registry,
        )  # fmt: skip
        self.approvals = Counter(
            "aios_approvals", "Approvals by how they ended", [*labels, "outcome"],
            registry=self.registry,
        )  # fmt: skip
        self.approval_seconds = Histogram(
            "aios_approval_wait_seconds", "Time an approval stayed open", [*labels, "outcome"],
            buckets=_APPROVAL_WAITS, registry=self.registry,
        )  # fmt: skip
        self._starts: dict[str, Event] = {}
        self._subscription: Subscription | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._subscription = await self.bus.subscribe(self.scope, ">")
        self._task = asyncio.create_task(self._consume(self._subscription))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._subscription is not None:
            await self._subscription.close()

    async def _consume(self, subscription: Subscription) -> None:
        with contextlib.suppress(BusClosedError):
            async for delivery in subscription:
                self.observe(delivery.event)
                await delivery.ack()

    def exposition(self) -> bytes:
        return generate_latest(self.registry)

    def _observe_model_call(self, tenant: str, app: str, data: dict[str, Any]) -> None:
        usage: dict[str, Any] = data.get("usage") or {}
        provider, model = str(data.get("provider")), str(data.get("model"))
        self.tokens.labels(tenant, app, provider, model, "input").inc(usage.get("input_tokens", 0))
        self.tokens.labels(tenant, app, provider, model, "output").inc(
            usage.get("output_tokens", 0)
        )
        self.cost.labels(tenant, app, provider, model).inc(usage.get("cost_usd", 0.0))
        self.cache_tokens.labels(tenant, app, provider, model, "read").inc(
            usage.get("cache_read_tokens", 0)
        )
        self.cache_tokens.labels(tenant, app, provider, model, "write").inc(
            usage.get("cache_write_tokens", 0)
        )
        if data.get("downgraded"):
            self.downgrades.labels(tenant, app, provider, model).inc()

    def observe(self, event: Event) -> None:
        data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
        tenant, app = event.tenantid, event.appid
        if event.type in ("run.started", "task.requested", "approval.requested"):
            self._starts[event.id] = event
        elif event.type in ("run.completed", "run.failed", "run.cancelled"):
            started = next(
                (
                    e
                    for e in self._starts.values()
                    if e.type == "run.started" and e.runid == event.runid
                ),
                None,
            )
            workflow = str(
                (started.data or {}).get("workflow")
                if started and isinstance(started.data, dict)
                else ""
            )
            self.runs.labels(tenant, app, workflow, event.type.split(".")[1]).inc()
            if started is not None:
                self._starts.pop(started.id, None)
                self.run_seconds.labels(tenant, app, workflow).observe(
                    (event.time - started.time).total_seconds()
                )
        elif event.type in _APPROVALS_ENDED:
            outcome = event.type.split(".")[1]
            self.approvals.labels(tenant, app, outcome).inc()
            started = self._starts.pop(event.causationid or "", None)
            if started is not None:
                self.approval_seconds.labels(tenant, app, outcome).observe(
                    (event.time - started.time).total_seconds()
                )
        elif event.type in ("task.completed", "task.failed", "task.suspended"):
            started = self._starts.pop(event.causationid or "", None)
            if started is not None:
                outcome = event.type.split(".")[1]
                self.task_seconds.labels(tenant, app, str(data.get("capability")), outcome).observe(
                    (event.time - started.time).total_seconds()
                )
        elif event.type == "model.call.completed":
            self._observe_model_call(tenant, app, data)
        elif event.type in ("tool.call.completed", "tool.call.failed"):
            self.tool_calls.labels(
                tenant, app, str(data.get("tool")), event.type.split(".")[2]
            ).inc()
        elif event.type == "system.dlq.added":
            self.dead_letters.labels(tenant, app, str(data.get("group"))).inc()
        elif event.type == "budget.exceeded":
            self.budget_stops.labels(
                tenant, app, str(data.get("budget")), str(data.get("level") or "")
            ).inc()
        elif event.type == "budget.warning":
            self.budget_warnings.labels(tenant, app, str(data.get("level"))).inc()

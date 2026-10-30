"""OpenTelemetry traces built from the event log (WP 1.6; FR-OBS-01, 02).

The tracer is a broadcast subscriber: it pairs start and end events and exports one span per
interval — the run, each step, each agent task, model call and tool call. Span ids are derived
from event ids and the trace id from the run's ``traceparent``, so spans nest correctly in
whatever order their events arrive, and Jaeger shows exactly what the log records. No
component is instrumented directly (ADR-001: runtime components can't import this one).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from importlib.metadata import version
from typing import Any, cast

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from aios.core.envelope import Event
from aios.core.errors import BusClosedError
from aios.fabric.port import EventBus, Scope, Subscription
from aios.observability import semconv

logger = logging.getLogger("aios.observability")

_ENDS = {
    "run.completed": "run",
    "run.failed": "run",
    "run.cancelled": "run",
    "step.completed": "step",
    "step.failed": "step",
    "task.completed": "task",
    "task.failed": "task",
    "tool.call.completed": "tool",
    "tool.call.failed": "tool",
}


def span_id(event_id: str) -> int:
    """A stable, non-zero 64-bit span id for the interval an event starts."""
    return int.from_bytes(hashlib.sha256(event_id.encode()).digest()[:8], "big") or 1


def trace_ids(event: Event) -> tuple[int, int | None] | None:
    """``(trace id, parent span id)`` from the event's W3C traceparent."""
    if event.traceparent is None:
        return None
    _, trace, parent, _ = event.traceparent.split("-")
    return int(trace, 16), int(parent, 16)


def _ns(when: datetime) -> int:
    return int(when.timestamp() * 1_000_000_000)


class EventTracer:
    def __init__(
        self,
        bus: EventBus,
        exporter: SpanExporter,
        *,
        scope: Scope | None = None,
        service: str = "aios",
    ) -> None:
        self.bus = bus
        self.exporter = exporter
        self.scope = scope or Scope.platform()
        self.resource = Resource.create(
            {"service.name": service, "service.version": version("aios")}
        )
        self._open: dict[str, Event] = {}
        self._step_spans: dict[tuple[str, str], str] = {}  # (run, step) -> step.started id
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
        self.exporter.force_flush()

    async def _consume(self, subscription: Subscription) -> None:
        with contextlib.suppress(BusClosedError):
            async for delivery in subscription:
                try:
                    spans = self.observe(delivery.event)
                    if spans:
                        self.exporter.export(spans)
                except Exception:
                    logger.exception("could not export spans for %s", delivery.event.id)
                await delivery.ack()

    def observe(self, event: Event) -> list[ReadableSpan]:
        """Feed one event; returns the spans it ends (public for tests and batch replays)."""
        if event.type in ("run.started", "step.started", "task.requested", "tool.call.requested"):
            self._open[event.id] = event
            if event.type == "step.started" and event.runid and isinstance(event.data, dict):
                self._step_spans[(event.runid, str(event.data.get("step")))] = event.id
            return []
        if event.type == "model.call.completed":
            return [self._model_span(event)]
        if event.type in _ENDS:
            start = self._start_of(event)
            if start is not None:
                return [self._interval(start, event)]
        return []

    def _start_of(self, end: Event) -> Event | None:
        kind = _ENDS[end.type]
        data: dict[str, Any] = end.data if isinstance(end.data, dict) else {}
        if kind == "run":
            key = next(
                (
                    k
                    for k, e in self._open.items()
                    if e.type == "run.started" and e.runid == end.runid
                ),
                None,
            )
        elif kind == "step":
            key = self._step_spans.get((end.runid or "", str(data.get("step"))))
        elif kind == "task":
            key = end.causationid
        else:
            key = next(
                (
                    k
                    for k, e in self._open.items()
                    if e.type == "tool.call.requested"
                    and isinstance(e.data, dict)
                    and e.data.get("site") == data.get("site")
                    and e.causationid == end.causationid
                ),
                None,
            )
        return self._open.pop(key, None) if key else None

    def _parent(self, start: Event) -> int | None:
        data: dict[str, Any] = start.data if isinstance(start.data, dict) else {}
        run = start.runid or ""
        if start.type == "run.started":
            ids = trace_ids(start)
            return ids[1] if ids else None
        if start.type == "step.started":
            return self._run_span(run)
        if start.type == "task.requested":
            step = self._step_spans.get((run, str(data.get("step"))))
            return span_id(step) if step else self._run_span(run)
        return span_id(start.causationid) if start.causationid else self._run_span(run)

    def _run_span(self, run_id: str) -> int | None:
        started = next(
            (e for e in self._open.values() if e.type == "run.started" and e.runid == run_id), None
        )
        return span_id(started.id) if started else None

    def _span(
        self,
        name: str,
        start_event: Event,
        *,
        own: int,
        parent: int | None,
        started: datetime,
        ended: datetime,
        attributes: Mapping[str, Any],
        failed: bool,
    ) -> ReadableSpan:
        ids = trace_ids(start_event)
        trace = ids[0] if ids else span_id(start_event.runid or start_event.id) << 64
        context = SpanContext(trace, own, is_remote=False, trace_flags=TraceFlags(1))
        parent_context = (
            SpanContext(trace, parent, is_remote=False, trace_flags=TraceFlags(1))
            if parent
            else None
        )
        base: dict[str, Any] = {
            semconv.TENANT: start_event.tenantid,
            semconv.APP: start_event.appid,
            semconv.EVENT_ID: start_event.id,
        }
        if start_event.runid:
            base[semconv.RUN_ID] = start_event.runid
        return ReadableSpan(
            name=name,
            context=context,
            parent=parent_context,
            resource=self.resource,
            attributes={**base, **{k: v for k, v in attributes.items() if v is not None}},
            kind=SpanKind.INTERNAL,
            status=Status(StatusCode.ERROR) if failed else Status(StatusCode.OK),
            start_time=_ns(started),
            end_time=_ns(max(ended, started)),
        )

    def _interval(self, start: Event, end: Event) -> ReadableSpan:
        data: dict[str, Any] = start.data if isinstance(start.data, dict) else {}
        end_data: dict[str, Any] = end.data if isinstance(end.data, dict) else {}
        failed = end.type.endswith((".failed", ".cancelled"))
        if start.type == "run.started":
            name, attributes = (
                f"run {data.get('workflow')}",
                {semconv.WORKFLOW: data.get("workflow")},
            )
        elif start.type == "step.started":
            name, attributes = f"step {data.get('step')}", {semconv.STEP: data.get("step")}
        elif start.type == "task.requested":
            agent = end.principal.split(":", 1)[1].split("@", 1)[0]
            name = f"{semconv.OP_INVOKE_AGENT} {agent}"
            attributes = {
                semconv.OPERATION: semconv.OP_INVOKE_AGENT,
                semconv.AGENT_NAME: agent,
                semconv.CAPABILITY: data.get("capability"),
                semconv.STEP: data.get("step"),
                semconv.COST_USD: cast(dict[str, Any], end_data.get("usage") or {}).get("cost_usd"),
            }
        else:
            name = f"{semconv.OP_EXECUTE_TOOL} {data.get('tool')}"
            attributes = {
                semconv.OPERATION: semconv.OP_EXECUTE_TOOL,
                semconv.TOOL_NAME: data.get("tool"),
            }
        return self._span(
            name,
            start,
            own=span_id(start.id),
            parent=self._parent(start),
            started=start.time,
            ended=end.time,
            attributes=attributes,
            failed=failed,
        )

    def _model_span(self, event: Event) -> ReadableSpan:
        data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
        usage: dict[str, Any] = data.get("usage") or {}
        latency = timedelta(milliseconds=float(data.get("latency_ms", 0.0)))
        parent = span_id(event.causationid) if event.causationid else None
        attributes = {
            semconv.OPERATION: semconv.OP_CHAT,
            semconv.PROVIDER: data.get("provider"),
            semconv.REQUEST_MODEL: data.get("model"),
            semconv.RESPONSE_MODEL: data.get("model"),
            semconv.INPUT_TOKENS: usage.get("input_tokens"),
            semconv.OUTPUT_TOKENS: usage.get("output_tokens"),
            semconv.COST_USD: usage.get("cost_usd"),
            semconv.AGENT_NAME: data.get("agent"),
            semconv.STEP: data.get("step"),
        }
        return self._span(
            f"{semconv.OP_CHAT} {data.get('model')}",
            event,
            own=span_id(event.id),
            parent=parent,
            started=event.time - latency,
            ended=event.time,
            attributes=attributes,
            failed=False,
        )


def spans_for(events: Sequence[Event], bus: EventBus, exporter: SpanExporter) -> list[ReadableSpan]:
    """All spans of a list of events (e.g. a replayed run)."""
    tracer = EventTracer(bus, exporter)
    spans: list[ReadableSpan] = []
    for event in events:
        spans += tracer.observe(event)
    return spans

"""The trace's own rules (FR-OBS-03): steps by cause, restricted data left out, approvals shown."""

from datetime import UTC, datetime, timedelta
from typing import Any

from aios.core.envelope import DataClass, Event
from aios.core.manifests import WorkflowManifest, load_manifests
from aios.observability.trace import REDACTED, trace_of

WORKFLOW = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: refund, version: 1.0.0}
spec:
  steps:
    - {id: check, type: task, capability: refunds.check}
    - {id: pay, type: task, capability: refunds.pay}
    - {id: tell, type: task, capability: refunds.tell, needs: [check]}
"""
START = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def workflow() -> WorkflowManifest:
    [found] = load_manifests(WORKFLOW)
    assert isinstance(found, WorkflowManifest)
    return found


class Run:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def add(
        self,
        type: str,
        data: dict[str, Any] | None = None,
        *,
        cause: Event | None = None,
        label: DataClass = DataClass.INTERNAL,
        tainted: bool = False,
    ) -> Event:
        event = Event(
            source="aios://acme/apps/desk",
            type=type,
            tenantid="acme",
            appid="desk",
            runid="01J0000000000000000000000R",
            correlationid="01J0000000000000000000000R",
            causationid=cause.id if cause else None,
            principal="agent:payer@acme",
            time=START + timedelta(seconds=len(self.events)),
            dataclass=label,
            tainted=tainted,
            data=data or {},
        )
        self.events.append(event)
        return event


def test_steps_follow_the_workflows_own_edges() -> None:
    trace = trace_of([], workflow(), app="desk")
    assert [n.id for n in trace.nodes] == ["check", "pay", "tell"]
    assert [(e.source, e.target) for e in trace.edges] == [("check", "pay"), ("check", "tell")]


def test_an_event_belongs_to_the_step_that_caused_it() -> None:
    run = Run()
    run.add("run.started", {"workflow": "refund", "version": "1.0.0"})
    started = run.add("step.started", {"step": "pay"})
    task = run.add("task.requested", {"step": "pay", "capability": "refunds.pay"}, cause=started)
    tool = run.add("tool.call.requested", {"tool": "pay.refund.create"}, cause=task)
    run.add(
        "policy.decision.denied",
        {"action": "tool.invoke", "resource": "pay.refund.create", "reason": "no"},
        cause=tool,
    )
    trace = trace_of(run.events, workflow(), app="desk")
    pay = next(n for n in trace.nodes if n.id == "pay")
    assert [c.tool for c in pay.tool_calls] == ["pay.refund.create"]
    assert [d.effect for d in pay.decisions] == ["denied"]
    assert [m.step for m in trace.timeline] == [None, "pay", "pay", "pay", "pay"]


def test_restricted_data_is_left_out() -> None:
    run = Run()
    started = run.add("step.started", {"step": "check"})
    run.add(
        "task.requested",
        {"step": "check", "capability": "x", "input": {"card": "4111"}},
        cause=started,
        label=DataClass.RESTRICTED,
    )
    run.add(
        "step.completed", {"step": "check", "output": {"card": "4111"}}, label=DataClass.RESTRICTED
    )
    check = next(n for n in trace_of(run.events, workflow(), app="desk").nodes if n.id == "check")
    assert check.input == REDACTED
    assert check.output == REDACTED
    assert "4111" not in trace_of(run.events, workflow(), app="desk").model_dump_json()


def test_confidential_data_is_shown_to_whoever_may_read_the_run() -> None:
    run = Run()
    run.add(
        "step.completed", {"step": "check", "output": {"order": 7}}, label=DataClass.CONFIDENTIAL
    )
    check = next(n for n in trace_of(run.events, workflow(), app="desk").nodes if n.id == "check")
    assert check.output == {"order": 7}


def test_an_approval_is_shown_on_its_step_with_its_answer() -> None:
    run = Run()
    started = run.add("step.started", {"step": "pay"})
    asked = run.add(
        "approval.requested",
        {"approval": "01J0000000000000000000000A", "summary": "Refund $120", "risk": "high"},
        cause=started,
    )
    pay = next(n for n in trace_of(run.events, workflow(), app="desk").nodes if n.id == "pay")
    assert pay.status == "waiting"
    run.add(
        "approval.granted",
        {"approval": "01J0000000000000000000000A", "by": "user:lee@acme", "comment": "fine"},
        cause=asked,
    )
    pay = next(n for n in trace_of(run.events, workflow(), app="desk").nodes if n.id == "pay")
    [approval] = pay.approvals
    assert (approval.status, approval.by, approval.comment) == ("granted", "user:lee@acme", "fine")
    assert pay.status == "running"


def test_a_failed_step_says_why() -> None:
    run = Run()
    run.add("step.started", {"step": "pay"})
    run.add("step.failed", {"step": "pay", "error": {"code": "budget.exceeded", "message": "over"}})
    run.add("run.failed", {})
    trace = trace_of(run.events, workflow(), app="desk")
    pay = next(n for n in trace.nodes if n.id == "pay")
    assert (pay.status, pay.error, trace.status) == ("failed", "budget.exceeded: over", "failed")
    assert pay.duration_ms == 1000


def test_taint_shows_on_the_step_that_read_it() -> None:
    run = Run()
    run.add("step.started", {"step": "check"})
    run.add(
        "memory.recalled",
        {"step": "check", "operation": "recall", "scopes": ["app"], "items": ["a", "b"]},
        tainted=True,
    )
    trace = trace_of(run.events, workflow(), app="desk")
    check = next(n for n in trace.nodes if n.id == "check")
    assert check.tainted
    assert trace.tainted
    assert [(m.operation, m.items) for m in check.memory] == [("recall", 2)]
    assert not next(n for n in trace.nodes if n.id == "pay").tainted


def test_a_step_the_graph_doesnt_have_is_still_shown() -> None:
    run = Run()
    run.add("step.started", {"step": "extra"})
    assert [n.id for n in trace_of(run.events, workflow(), app="desk").nodes][-1] == "extra"


def test_without_a_workflow_the_events_still_make_steps() -> None:
    run = Run()
    run.add("step.started", {"step": "only"})
    trace = trace_of(run.events, None, app="desk")
    assert ([n.id for n in trace.nodes], trace.edges) == (["only"], [])

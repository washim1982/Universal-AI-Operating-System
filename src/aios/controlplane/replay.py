"""Replays, forks and retries: what a new run keeps of an old one (ADR-021; FR-OBS-04, FR-WF-11).

Each is a new run, so a finished run stays finished and the log is only ever added to. The new
run names its source, and keeps the source's data label and every source of untrusted content it
had: what it copies, it copies the taint of, so a fork can't launder what a run read.

People and outside signals aren't asked again in a replay or a fork. Their answers came from
outside the run, like a tool's output, so the source's are kept. A retry asks again: it is real.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, Literal

from pydantic import JsonValue

from aios.core.catalog import AppRequestReceived, CopiedStep, RunSource
from aios.core.errors import ConflictError, NotFoundError
from aios.core.manifests import ApprovalStep, WaitStep, WorkflowManifest
from aios.workflow import RunState

if TYPE_CHECKING:
    from aios.controlplane.runtime import Runtime

Mode = Literal["replay", "fork", "retry"]
KEEP: Any = object()
"""The source's own input: a fork that leaves the input alone."""


def needs(workflow: WorkflowManifest) -> dict[str, list[str]]:
    """Each step's needs, as the engine reads them: the step before, unless it says otherwise."""
    steps = workflow.spec.steps
    return {
        step.id: list(step.needs) if step.needs is not None else ([steps[i - 1].id] if i else [])
        for i, step in enumerate(steps)
    }


def again(workflow: WorkflowManifest, start: str) -> set[str]:
    """``start`` and every step that comes after it: what a fork or retry runs again."""
    after: dict[str, list[str]] = {step.id: [] for step in workflow.spec.steps}
    for step, before in needs(workflow).items():
        for need in before:
            after[need].append(step)
    found, queue = {start}, deque([start])
    while queue:
        for step in after[queue.popleft()]:
            if step not in found:
                found.add(step)
                queue.append(step)
    return found


def kept(state: RunState, workflow: WorkflowManifest, rerun: set[str]) -> list[CopiedStep]:
    """The source's results for the steps that don't run again, in workflow order."""
    return [
        CopiedStep(step=step.id, output=found.output, loop=max(found.runs - 1, 0))
        for step in workflow.spec.steps
        if step.id not in rerun
        and (found := state.steps.get(step.id)) is not None
        and found.status == "completed"
    ]


def answers(
    state: RunState, workflow: WorkflowManifest, rerun: set[str], mode: Mode
) -> list[CopiedStep]:
    """What people and signals said in the source, for the steps that run again. A retry asks
    again, so it has none: it is real."""
    if mode == "retry":
        return []
    outside = [step for step in workflow.spec.steps if isinstance(step, (ApprovalStep, WaitStep))]
    return [
        CopiedStep(step=step.id, output=found.output, loop=max(found.runs - 1, 0))
        for step in outside
        if step.id in rerun
        and (found := state.steps.get(step.id)) is not None
        and found.status == "completed"
    ]


async def request_for(
    runtime: Runtime,
    tenant: str,
    app: str,
    run_id: str,
    mode: Mode,
    *,
    step: str | None = None,
    input: JsonValue = KEEP,
    step_input: JsonValue = None,
    model: str | None = None,
) -> tuple[AppRequestReceived, RunState]:
    """The request that starts a replay, fork or retry of ``run_id``, and the source's state."""
    state = await runtime.run_state(app, run_id, tenant=tenant)
    if state.events == 0:
        raise NotFoundError(f"no run {run_id}")
    if not state.finished:
        raise ConflictError(
            f"run {run_id} is {state.status}: only a finished run has all its record"
        )
    definition = runtime.catalog.get(tenant, app)
    workflow = definition.workflow(state.workflow, state.version) if definition else None
    if workflow is None or workflow.metadata.version != state.version:
        raise NotFoundError(f"{app} no longer has {state.workflow} {state.version}")
    ids = [found.id for found in workflow.spec.steps]
    if mode == "replay":
        step = None
    elif step is None:
        failed = [found for found in ids if (s := state.steps.get(found)) and s.status == "failed"]
        if not failed:
            raise ConflictError("nothing failed in that run: say which step to start from")
        step = failed[0]
    elif step not in ids:
        raise NotFoundError(f"{state.workflow} has no step {step!r}")
    if model is not None and runtime.models.registry.get(model) is None:
        raise NotFoundError(f"no model {model!r} in the registry")
    rerun = set(ids) if step is None else again(workflow, step)
    source = RunSource(
        run=run_id,
        mode=mode,
        step=step,
        copied=kept(state, workflow, rerun),
        answers=answers(state, workflow, rerun, mode),
        taint=sorted(state.taint),
        step_input=step_input if mode != "replay" else None,
        model=model if mode != "replay" else None,
        live=mode == "retry",
    )
    request = AppRequestReceived(
        entrypoint=mode,
        workflow=state.workflow,
        version=state.version,
        input=state.input if input is KEEP or mode == "replay" else input,
        source=source,
    )
    return request, state

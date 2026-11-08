"""Asking an App and waiting for its answer, for clients that expect one (FR-INT-01, 03; WP 3.7).

An OpenAI-compatible client or an MCP client sends a message and wants an answer back. A run can
take long, or wait for a person, so asking waits until the run ends or waits for someone, or until
a timeout, and then says where it stands. A run never stops because nobody is waiting for it: the
caller can ask again with the run's id.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import JsonValue

from aios.core.envelope import DataClass
from aios.core.errors import NotFoundError, PolicyDeniedError
from aios.core.identity import Identity
from aios.core.manifests import ChatEntrypoint
from aios.core.policy import PolicyAction
from aios.fabric.port import Scope
from aios.workflow.state import RunState

if TYPE_CHECKING:
    from aios.controlplane.runtime import Runtime

FIRST_POLL_SECONDS = 0.05
LAST_POLL_SECONDS = 1.0
"""How often to look again: soon at first, as most answers are quick; then less often."""

ANSWER_KEYS = ("answer", "reply", "text", "content", "message")
"""An output that is an object says its answer under one of these, if it has one."""

Outcome = Literal["completed", "failed", "cancelled", "waiting", "running"]


@dataclass(frozen=True, slots=True)
class Asked:
    app: str
    run_id: str
    status: Outcome
    output: JsonValue = None
    error: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict[str, Any])
    waiting: list[str] = field(default_factory=list[str])
    """What the run waits for, in words: approvals, people's tasks, signals."""

    @property
    def trace(self) -> str:
        return f"/v1/apps/{self.app}/runs/{self.run_id}"

    @property
    def text(self) -> str:
        """What a person reads: the answer, or why there is none yet."""
        return self.say(f"ask about run {self.run_id} again later")

    def say(self, again: str) -> str:
        """:attr:`text`, with ``again`` saying how to ask again when there is no answer yet."""
        if self.status == "completed":
            return answer_text(self.output)
        if self.status == "waiting":
            return (
                f"Run {self.run_id} is waiting for {'; '.join(self.waiting)}. It goes on once "
                f"that is done; {again}."
            )
        if self.status == "running":
            return f"Run {self.run_id} is still working; {again}."
        reason = (self.error or {}).get("message") or self.status
        return f"Run {self.run_id} {self.status}: {reason}"

    def view(self) -> dict[str, JsonValue]:
        return {
            "app": self.app,
            "run_id": self.run_id,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "waiting": list(self.waiting),
            "usage": self.usage,
            "trace": self.trace,
        }


def chat_apps(runtime: Runtime, tenant: str) -> dict[str, str]:
    """The Apps of ``tenant`` with a chat entry point, by name, with their descriptions."""
    return {
        name: installed.manifest.spec.description
        for (owner, name), installed in sorted(runtime.apps.items())
        if owner == tenant
        and any(isinstance(e, ChatEntrypoint) for e in installed.manifest.spec.entrypoints)
    }


def answer_text(output: JsonValue) -> str:
    """An App's output as text: itself, its ``answer`` (or reply, text...), or its JSON."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ANSWER_KEYS:
            if isinstance(output.get(key), str):
                return str(output[key])
    return "```json\n" + json.dumps(output, indent=2, ensure_ascii=False) + "\n```"


async def start(
    runtime: Runtime,
    by: Identity,
    app: str,
    message: str,
    *,
    input: JsonValue = None,
    session: str | None = None,
    data_class: DataClass = DataClass.INTERNAL,
    traceparent: str | None = None,
    via: Literal["openai", "mcp"] | None = None,
    authorized: bool = False,
) -> str:
    """Start a run of ``app``'s chat entry point for ``by``; return its id.

    Raises :class:`PolicyDeniedError` when ``by`` may not, and :class:`NotFoundError` when there
    is no such App.
    """
    if not authorized:
        decision = await runtime.authorize(by, PolicyAction.RUN_START, app=app)
        if not decision.allowed:
            raise PolicyDeniedError(decision.reason or "run.start is not allowed")
    return await runtime.chat(
        app,
        message,
        input=input,
        tenant=by.tenant,
        principal=by.principal,
        session=session,
        data_class=data_class,
        traceparent=traceparent,
        via=via,
    )


async def ask(
    runtime: Runtime,
    by: Identity,
    app: str,
    message: str,
    *,
    input: JsonValue = None,
    session: str | None = None,
    data_class: DataClass = DataClass.INTERNAL,
    timeout: float,
) -> Asked:
    """Start a run, as :func:`start` does, and wait for it, as :func:`outcome` does."""
    run_id = await start(
        runtime, by, app, message, input=input, session=session, data_class=data_class
    )
    return await outcome(runtime, by, app, run_id, timeout=timeout, authorized=True)


async def outcome(
    runtime: Runtime,
    by: Identity,
    app: str,
    run_id: str,
    *,
    timeout: float,
    authorized: bool = False,
) -> Asked:
    """Wait until the run ends or waits for someone, at most ``timeout`` seconds; say how it
    stands. Raises :class:`NotFoundError` for a run that isn't there."""
    if not authorized:
        decision = await runtime.authorize(by, PolicyAction.RUN_READ, app=app)
        if not decision.allowed:
            raise PolicyDeniedError(decision.reason or "run.read is not allowed")
        runtime.app(app, tenant=by.tenant)  # NotFoundError
    scope = Scope(by.tenant, app)
    state = RunState(run_id)
    deadline = time.monotonic() + timeout
    pause = FIRST_POLL_SECONDS
    while True:
        async for stored in runtime.bus.replay(scope, run_id=run_id, after=state.last_position):
            state.apply(stored)
        if not authorized and state.events == 0:
            raise NotFoundError(f"no run {run_id}")
        waiting = await _waiting_for(runtime, by.tenant, run_id, state)
        if state.ended is not None or waiting or time.monotonic() >= deadline:
            return _asked(app, run_id, state, waiting)
        await asyncio.sleep(min(pause, max(0.0, deadline - time.monotonic())))
        pause = min(pause * 2, LAST_POLL_SECONDS)


async def _waiting_for(runtime: Runtime, tenant: str, run_id: str, state: RunState) -> list[str]:
    if state.ended is not None:
        return []
    found: list[str] = []
    if state.status == "paused":
        for approval in await runtime.human.store.in_run(tenant, run_id):
            who = ", ".join(approval.approvers)
            found.append(f"approval {approval.id} ({approval.details.summary}) from {who}")
        found += [
            f"a signal at step {reason.split(':', 1)[1]}"
            for reason in sorted(state.waiting)
            if reason.startswith("signal:")
        ]
    if runtime.human.tasks is not None:
        found += [
            f"task {task.id} ({task.details.capability}) by {', '.join(task.details.assignees)}"
            for task in await runtime.human.tasks.in_run(tenant, run_id)
        ]
    return found


def _asked(app: str, run_id: str, state: RunState, waiting: list[str]) -> Asked:
    end = state.ended
    data: dict[str, Any] = end.data if end is not None and isinstance(end.data, dict) else {}
    status: Outcome
    if end is None:
        status = "waiting" if waiting else "running"
    elif end.type == "run.completed":
        status = "completed"
    elif end.type == "run.cancelled":
        status = "cancelled"
    else:
        status = "failed"
    return Asked(
        app=app,
        run_id=run_id,
        status=status,
        output=data.get("output"),
        error=data.get("error"),
        usage=data.get("usage")
        or {
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
            "cost_usd": round(state.cost_usd, 6),
        },
        waiting=waiting,
    )

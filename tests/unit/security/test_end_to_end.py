"""The Security Kernel across real runs: decisions, delegation, kill switch and audit chain."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import BaseModel

from aios.core.catalog import PolicyDecided
from aios.core.envelope import DataClass, Event
from aios.core.identity import Identity, Suspendable
from aios.core.manifests import load_manifests
from aios.core.policy import PolicyDecision, PolicyRequest, SideEffect
from aios.fabric import Scope
from aios.kernel import Context, agent
from aios.security.audit import AuditLog
from aios.security.identity import ApiKeys
from aios.testing import task_request
from aios.testing.runtime import AGENTS_APP, LocalRuntime, RunHandle, RunResult
from aios.tools import NativeProvider, tool

ANONYMOUS = "user:anonymous"
TENANT = "acme"
OPERATOR = Identity("system:platform", TENANT)
ALICE = "user:alice@acme"
APP = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 0.1.0}
spec:
  entrypoints: [{type: chat, workflow: act}]
  workflows: [act]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: act, version: 0.1.0}
spec:
  steps:
    - {id: write, type: tool, tool: crm.note.add, input: {text: hello}}
"""
RESTRICTED = """
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: lock-notes, version: 1.0.0}
spec:
  subject: {app: desk}
  deny:
    - {action: tool.invoke, resource: crm.note.add}
"""


class Calls:
    def __init__(self) -> None:
        self.count = 0

    def provider(self) -> NativeProvider:
        calls = self

        @tool("crm.note.add", side_effect=SideEffect.WRITE)
        async def add(text: str) -> dict[str, str]:
            calls.count += 1
            return {"added": text}

        return NativeProvider("crm", [add])


async def _desk(runtime: LocalRuntime, *policies: str, tenant: str = TENANT) -> Calls:
    manifests = load_manifests(APP + "".join(f"---{policy}" for policy in policies))
    await runtime.runtime.apply(manifests, tenant=tenant)
    calls = Calls()
    await runtime.runtime.register_tool_provider(calls.provider())
    return calls


async def _run(
    runtime: LocalRuntime,
    principal: str = ANONYMOUS,
    data_class: DataClass = DataClass.INTERNAL,
) -> RunResult:
    run = await runtime.runtime.chat(
        "desk", "go", tenant=TENANT, principal=principal, data_class=data_class
    )
    return await RunHandle(runtime, "desk", run).wait(timeout=10)


async def _settled_task(runtime: LocalRuntime, run: str | None) -> list[Event]:
    scope = Scope(TENANT, AGENTS_APP)
    async with asyncio.timeout(5):
        while True:
            seen = [s.event async for s in runtime.bus.replay(scope, run_id=run)]
            if any(e.type in ("task.completed", "task.failed") for e in seen):
                return seen
            await asyncio.sleep(0.01)


def _types(events: list[Event]) -> list[str]:
    return [e.type for e in events]


# --------------------------------------------------------------------------- decisions


async def test_an_allowed_call_leaves_a_decision_on_the_record() -> None:
    async with LocalRuntime() as runtime:
        calls = await _desk(runtime)
        result = await _run(runtime)
    assert result.status == "completed"
    assert calls.count == 1
    decided = next(e for e in result.events if e.type == "policy.decision.allowed")
    assert decided.data == {
        "action": "tool.invoke",
        "resource": "crm.note.add",
        "policies": ["tool-runner/allow/0"],
        "approvers": [],
        "reason": "",
        "approval": None,
    }


async def test_an_app_policy_stops_a_workflow_tool() -> None:
    async with LocalRuntime() as runtime:
        calls = await _desk(runtime, RESTRICTED)
        result = await _run(runtime)
    assert calls.count == 0
    assert result.status == "failed"
    denied = next(e for e in result.events if e.type == "policy.decision.denied")
    assert PolicyDecided.model_validate(denied.data).policies == ["lock-notes/deny/0"]
    assert "tool.call.completed" not in _types(result.events)


# --------------------------------------------------------------------------- delegation


async def test_a_run_inherits_its_callers_limits() -> None:
    async with LocalRuntime() as runtime:
        calls = await _desk(runtime)
        await runtime.runtime.security.set_roles(TENANT, ALICE, frozenset({"auditor"}))
        result = await _run(runtime, ALICE)
    assert calls.count == 0
    assert result.status == "failed"
    assert {e.onbehalfof for e in result.events} == {ALICE}


async def test_a_member_can_use_the_tools_of_an_app() -> None:
    async with LocalRuntime() as runtime:
        calls = await _desk(runtime)
        await runtime.runtime.security.set_roles(TENANT, ALICE, frozenset({"member"}))
        result = await _run(runtime, ALICE)
    assert calls.count == 1
    assert result.status == "completed"


async def test_a_run_started_with_an_api_key_can_use_tools() -> None:
    """A worker finds the key's roles in the directory, not in the request that signed in."""
    async with LocalRuntime() as runtime:
        calls = await _desk(runtime)
        _, value = await runtime.runtime.create_key(OPERATOR, "ci", ["member"])
        caller = await ApiKeys(runtime.runtime.security).authenticate(value)
        result = await _run(runtime, caller.principal)
    assert result.status == "completed"
    assert calls.count == 1


async def test_a_run_started_with_a_key_stops_when_the_key_is_revoked() -> None:
    async with LocalRuntime() as runtime:
        calls = await _desk(runtime)
        key, value = await runtime.runtime.create_key(OPERATOR, "ci", ["member"])
        caller = await ApiKeys(runtime.runtime.security).authenticate(value)
        await runtime.runtime.revoke_key(OPERATOR, key.id)
        result = await _run(runtime, caller.principal)
    assert result.status == "failed"
    assert calls.count == 0


async def test_an_anonymous_run_has_no_delegate() -> None:
    async with LocalRuntime() as runtime:
        await _desk(runtime)
        result = await _run(runtime)
    assert {e.onbehalfof for e in result.events} == {None}


# --------------------------------------------------------------------------- the kill switch


async def test_a_suspended_agent_gets_no_work_and_the_run_says_why() -> None:
    async with LocalRuntime() as runtime:
        calls = await _desk(runtime)
        await runtime.runtime.suspend(
            OPERATOR, Suspendable.AGENT, "tool-runner", reason="runaway agent"
        )
        result = await _run(runtime)
    assert calls.count == 0
    assert result.status == "failed"
    assert "runaway agent" in str(result.error)


async def test_a_suspended_tenant_stops_a_worker_at_once() -> None:
    async with LocalRuntime() as runtime:
        calls = await _desk(runtime)
        await runtime.runtime.suspend(OPERATOR, Suspendable.TENANT, TENANT, reason="breach")
        result = await _run(runtime)
    assert calls.count == 0
    assert result.status == "failed"


async def test_lifting_a_suspension_restores_work() -> None:
    async with LocalRuntime() as runtime:
        calls = await _desk(runtime)
        await runtime.runtime.suspend(OPERATOR, Suspendable.APP, "desk", reason="incident")
        assert await runtime.runtime.lift_suspension(OPERATOR, Suspendable.APP, "desk")
        result = await _run(runtime)
    assert calls.count == 1
    assert result.status == "completed"


class Question(BaseModel):
    message: str


@agent("model-agent", capability="asking")
async def ask(ctx: Context, task: Question) -> str:
    return await ctx.model.generate(messages=task.message)


async def test_a_suspension_stops_a_model_call_already_on_its_way() -> None:
    """Dispatch was allowed, then the switch was thrown: the next model call is refused."""
    async with LocalRuntime(agents=[ask]) as runtime:
        real = runtime.runtime.policy.decide
        calls: list[str] = []

        async def suspend_after_dispatch(request: PolicyRequest) -> PolicyDecision:
            calls.append(request.action.value)
            decision = await real(request)
            if request.action.value == "task.dispatch":
                await runtime.runtime.suspend(
                    OPERATOR, Suspendable.AGENT, "model-agent", reason="caught it"
                )
            return decision

        runtime.runtime.policy.decide = suspend_after_dispatch  # type: ignore[method-assign]
        request = task_request("asking", {"message": "hi"}, tenantid=TENANT, appid=AGENTS_APP)
        await runtime.bus.publish(request)
        seen = await _settled_task(runtime, request.runid)
    assert calls == ["task.dispatch", "model.route"]
    failed = next(e for e in seen if e.type == "task.failed")
    assert "caught it" in str(failed.data)
    assert "model.call.failed" in _types(seen)


# --------------------------------------------------------------------------- reserved events


@pytest.mark.parametrize(
    "type",
    [
        "approval.granted",
        "task.completed",
        "run.cancel.requested",
        "tool.call.completed",
        "policy.decision.allowed",
        "security.resumed",
        "system.dlq.added",
    ],
)
async def test_an_agent_can_not_forge_a_platform_event(type: str) -> None:
    @agent("forger", capability="forging")
    async def forge(ctx: Context, task: Any) -> None:
        await ctx.emit(type, {"approval": "01J0000000000000000000000A"})

    async with LocalRuntime(agents=[forge]) as runtime:
        request = task_request("forging", tenantid=TENANT, appid=AGENTS_APP)
        await runtime.bus.publish(request)
        seen = await _settled_task(runtime, request.runid)
    assert type not in _types(seen)
    failed = next(e for e in seen if e.type == "task.failed")
    assert isinstance(failed.data, dict)
    assert failed.data["error"]["code"] == "event.reserved"  # type: ignore[index, call-overload]


async def test_an_agent_can_still_emit_its_own_events() -> None:
    @agent("reporter", capability="reporting")
    async def report(ctx: Context, task: Any) -> None:
        await ctx.emit("research.findings.recorded", {"ok": True})

    async with LocalRuntime(agents=[report]) as runtime:
        request = task_request("reporting", tenantid=TENANT, appid=AGENTS_APP)
        await runtime.bus.publish(request)
        seen = await _settled_task(runtime, request.runid)
    assert "research.findings.recorded" in _types(seen)
    assert "task.failed" not in _types(seen)


# --------------------------------------------------------------------------- data classes


async def test_a_labelled_run_keeps_its_label_on_every_event() -> None:
    async with LocalRuntime() as runtime:
        await _desk(runtime)
        result = await _run(runtime, data_class=DataClass.RESTRICTED)
    assert {e.dataclass for e in result.events} == {DataClass.RESTRICTED}


# --------------------------------------------------------------------------- the audit chain


async def _settled(log: AuditLog, tenant: str, type: str) -> None:
    async with asyncio.timeout(5):
        while not any(  # noqa: ASYNC110 - the audit log has no event to wait on
            e.event.type == type for e in await log.entries(tenant, limit=10_000)
        ):
            await asyncio.sleep(0.01)


async def test_a_run_leaves_a_verifiable_chain() -> None:
    async with LocalRuntime() as runtime:
        await _desk(runtime)
        await runtime.runtime.security.set_roles(TENANT, ALICE, frozenset({"member"}))
        await _run(runtime, ALICE)
        log = runtime.runtime.audit
        await _settled(log, TENANT, "tool.call.completed")
        report = await log.verify(TENANT)
        types = {e.event.type for e in await log.entries(TENANT)}
    assert report.ok
    assert {
        "app.request.received",
        "tool.call.requested",
        "policy.decision.allowed",
        "tool.call.completed",
    } <= types


async def test_keys_and_suspensions_are_on_the_record() -> None:
    async with LocalRuntime() as runtime:
        rt = runtime.runtime
        admin = Identity("user:ops@acme", TENANT, frozenset({"tenant-admin"}))
        key, _ = await rt.create_key(admin, "ci", ["member"])
        await rt.revoke_key(admin, key.id)
        await rt.suspend(admin, Suspendable.AGENT, "x", reason="test")
        await rt.lift_suspension(admin, Suspendable.AGENT, "x")
        await _settled(rt.audit, TENANT, "security.resumed")
        entries = [e.event for e in await rt.audit.entries(TENANT)]
    assert [e.type for e in entries] == [
        "security.key.created",
        "security.key.revoked",
        "security.suspended",
        "security.resumed",
    ]
    assert {e.principal for e in entries} == {"user:ops@acme"}


async def test_the_audit_entry_of_a_secret_tool_holds_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "XXXXXX"
    monkeypatch.setenv("AIOS_SECRET_NOTE_KEY", secret)

    @tool("crm.note.add", side_effect=SideEffect.WRITE, credentials=["NOTE_KEY"])
    async def add(text: str, credentials: Mapping[str, str]) -> dict[str, str]:
        return {"sent": f"Bearer {credentials['NOTE_KEY']}"}

    async with LocalRuntime() as runtime:
        manifest = APP.replace(
            "workflows: [act]", "workflows: [act]\n  config: {secrets: [NOTE_KEY]}"
        )
        await runtime.runtime.apply(load_manifests(manifest), tenant=TENANT)
        await runtime.runtime.register_tool_provider(NativeProvider("crm", [add]))
        await _run(runtime)
        log = runtime.runtime.audit
        await _settled(log, TENANT, "tool.call.completed")
        text = " ".join(e.entry for e in await log.entries(TENANT))
    assert "REDACTED" in text
    assert secret not in text

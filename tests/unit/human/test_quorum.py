"""Quorum, groups, reminders and escalation (FR-HUM-04, FR-HUM-05; WP 3.5)."""

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from types import ModuleType
from typing import Any

import pytest

from aios.core.errors import ApprovalClosedError, ApprovalForbiddenError
from aios.core.human import Approval
from aios.core.identity import Identity
from aios.core.manifests import load_manifests
from aios.core.policy import SideEffect
from aios.fabric import Scope
from aios.kernel import Context
from aios.testing.runtime import LocalRuntime, RunHandle
from aios.tools import NativeProvider, tool

VAULT = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: vault, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: pay}]
  agents: [clerk]
  workflows: [pay]
  policies: [payments]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: clerk, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "quorum_vault:pay"}
  capabilities: [{name: vault.pay}]
  tools: {allow: ["vault.>"]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: pay, version: 1.0.0}
spec:
  steps:
%s
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: payments, version: 1.0.0}
spec:
  subject: {app: vault}
  allow:
    - {action: tool.invoke, resource: vault.transfer.create}
  requireApproval:
    - action: tool.invoke
      resource: vault.transfer.create
      approvers: ["group:finance"]
%s
"""
PAYS = "    - {id: pay, type: task, capability: vault.pay, input: {amount: 900}}"
TWO = "      quorum: 2"
ANN = Identity("user:ann@acme", "acme", frozenset({"member"}))
BOB = Identity("user:bob@acme", "acme", frozenset({"member"}))
EVE = Identity("user:eve@acme", "acme", frozenset({"member"}))
CFO = Identity("user:cfo@acme", "acme", frozenset({"member", "cfo"}))
ADMIN = Identity("system:platform", "acme")


async def _pay(ctx: Context, task: dict[str, Any]) -> Any:
    return await ctx.tools.invoke("vault.transfer.create", amount=task["amount"])


Vault = tuple[LocalRuntime, list[float]]


@pytest.fixture
async def vault(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Callable[..., Awaitable[Vault]]]:
    module = ModuleType("quorum_vault")
    module.pay = _pay  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "quorum_vault", module)
    running: list[LocalRuntime] = []

    async def make(terms: str = TWO, steps: str = PAYS) -> Vault:
        sent: list[float] = []
        runtime = LocalRuntime(tenant="acme")
        await runtime.__aenter__()
        running.append(runtime)

        @tool("vault.transfer.create", side_effect=SideEffect.FINANCIAL, trusted=True)
        async def transfer(amount: float) -> dict[str, Any]:
            sent.append(amount)
            return {"sent": amount}

        await runtime.runtime.register_tool_provider(NativeProvider("vault", [transfer]))
        await runtime.runtime.apply(load_manifests(VAULT % (steps, terms)), tenant="acme")
        await runtime.runtime.set_group(ADMIN, "finance", ["user:ann@acme", "user:bob@acme"])
        return runtime, sent

    yield make
    for runtime in running:
        await runtime.__aexit__(None, None, None)


async def asked(runtime: LocalRuntime) -> tuple[RunHandle, Approval]:
    handle = await runtime.chat("vault", "pay it")
    event = await handle.wait_for_event("approval.requested", timeout=10)
    data = event.data if isinstance(event.data, dict) else {}
    core = runtime.runtime
    async with asyncio.timeout(10):
        while True:
            found = await core.human.get("acme", str(data["approval"]))
            if found is not None:
                return handle, found
            await asyncio.sleep(0.02)


async def published(runtime: LocalRuntime, run: str, type: str) -> list[dict[str, Any]]:
    scope = Scope("acme", "vault")
    events = [s.event async for s in runtime.runtime.bus.replay(scope, type, run_id=run)]
    return [e.data for e in events if isinstance(e.data, dict)]


async def test_a_quorum_needs_that_many_different_people(vault: Any) -> None:
    runtime, sent = await vault()
    human = runtime.runtime.human
    handle, approval = await asked(runtime)
    assert approval.quorum == 2
    first = await human.decide(ANN, approval.id, approve=True, comment="checked the invoice")
    assert (first.status, first.voted) == ("pending", ("user:ann@acme",))
    with pytest.raises(ApprovalClosedError, match="already"):
        await human.decide(ANN, approval.id, approve=True)
    assert [a.id for a in await human.inbox(ANN)] == []  # nothing left for ann to do
    assert sent == []
    granted = await human.decide(BOB, approval.id, approve=True)
    assert granted.status == "granted"
    assert (await handle.wait(timeout=10)).status == "completed"
    assert sent == [900]
    votes = await published(runtime, handle.run_id, "approval.voted")
    assert [(v["by"], v["votes"], v["quorum"]) for v in votes] == [
        ("user:ann@acme", 1, 2),
        ("user:bob@acme", 2, 2),
    ]
    [final] = await published(runtime, handle.run_id, "approval.granted")
    assert final["voters"] == ["user:ann@acme", "user:bob@acme"]


async def test_with_a_quorum_nobody_changes_the_action(vault: Any) -> None:
    """Everyone must agree to the same thing: a change by one voter would be the only one."""
    runtime, sent = await vault()
    human = runtime.runtime.human
    handle, approval = await asked(runtime)
    await human.decide(ANN, approval.id, approve=True)
    with pytest.raises(ApprovalForbiddenError, match="with a quorum"):
        await human.decide(BOB, approval.id, approve=True, edits={"amount": 9000})
    assert (await human.get("acme", approval.id)).voted == ("user:ann@acme",)  # type: ignore[union-attr]
    await human.decide(BOB, approval.id, approve=True)
    await handle.wait(timeout=10)
    assert sent == [900]


async def test_one_no_is_enough(vault: Any) -> None:
    runtime, sent = await vault()
    human = runtime.runtime.human
    handle, approval = await asked(runtime)
    await human.decide(ANN, approval.id, approve=True)
    denied = await human.decide(BOB, approval.id, approve=False, comment="wrong account")
    assert denied.status == "denied"
    await handle.wait(timeout=10)
    assert sent == []


async def test_a_group_is_the_directorys_or_the_identity_providers(vault: Any) -> None:
    runtime, _ = await vault()
    human = runtime.runtime.human
    _, approval = await asked(runtime)
    with pytest.raises(ApprovalForbiddenError):
        await human.decide(EVE, approval.id, approve=True)
    claimed = Identity("user:carl@acme", "acme", frozenset({"member"}), frozenset({"finance"}))
    assert [a.id for a in await human.inbox(claimed)] == [approval.id]
    voted = await human.decide(claimed, approval.id, approve=True)
    assert voted.voted == ("user:carl@acme",)


async def test_nobody_answers_so_it_escalates(vault: Any) -> None:
    escalates = TWO + '\n      escalate:\n        - {after: 1h, approvers: ["role:cfo"]}'
    runtime, _ = await vault(escalates)
    human = runtime.runtime.human
    handle, approval = await asked(runtime)
    with pytest.raises(ApprovalForbiddenError):
        await human.decide(CFO, approval.id, approve=True)
    assert await human.nudge(approval.request.time + timedelta(minutes=59)) == 0
    assert await human.nudge(approval.request.time + timedelta(hours=1)) == 1
    assert await human.nudge(approval.request.time + timedelta(hours=1)) == 0  # done already
    [escalated] = await published(runtime, handle.run_id, "approval.escalated")
    assert (escalated["level"], escalated["approvers"]) == (1, ["role:cfo"])
    voted = await human.decide(CFO, approval.id, approve=True)
    assert voted.voted == ("user:cfo@acme",)


async def test_whoever_it_waits_for_is_reminded(vault: Any) -> None:
    runtime, _ = await vault(TWO + "\n      remindEvery: 30m")
    human = runtime.runtime.human
    handle, approval = await asked(runtime)
    start = approval.request.time
    assert await human.nudge(start + timedelta(minutes=29)) == 0
    assert await human.nudge(start + timedelta(minutes=30)) == 1
    assert await human.nudge(start + timedelta(minutes=45)) == 0
    assert await human.nudge(start + timedelta(minutes=61)) == 1
    counts = [r["count"] for r in await published(runtime, handle.run_id, "approval.reminded")]
    assert counts == [1, 2]


async def test_an_approval_step_needs_its_quorum_too(vault: Any) -> None:
    steps = (
        '    - {id: check, type: approval, approvers: ["group:finance"], quorum: 2}\n'
        + PAYS.replace("{id: pay,", "{id: pay, needs: [check],")
    )
    runtime, sent = await vault("      quorum: 1", steps)
    human = runtime.runtime.human
    handle, approval = await asked(runtime)
    assert approval.details.step == "check"
    await human.decide(ANN, approval.id, approve=True)
    state = await runtime.runtime.run_state("vault", handle.run_id, tenant="acme")
    assert state.status == "paused"
    await human.decide(BOB, approval.id, approve=True)
    # the step is answered; the payment itself needs its own approval
    async with asyncio.timeout(10):
        while True:
            if len(await published(runtime, handle.run_id, "approval.requested")) == 2:
                break
            await asyncio.sleep(0.02)
    completed = await published(runtime, handle.run_id, "step.completed")
    assert [c["step"] for c in completed] == ["check"]
    assert sent == []


async def test_a_tenants_groups_can_be_set_and_emptied(vault: Any) -> None:
    runtime, _ = await vault()
    core = runtime.runtime
    assert await core.groups("acme") == {"finance": frozenset({"user:ann@acme", "user:bob@acme"})}
    await core.set_group(ADMIN, "finance", [])
    assert await core.groups("acme") == {}

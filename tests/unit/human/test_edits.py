"""Approve with edits (FR-HUM-06) and dry-run previews (FR-TOOL-07; WP 3.5)."""

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from types import ModuleType
from typing import Any

import pytest

from aios.core.errors import ApprovalForbiddenError
from aios.core.human import Approval
from aios.core.identity import Identity
from aios.core.manifests import load_manifests
from aios.core.policy import SideEffect
from aios.fabric import Scope
from aios.kernel import Context
from aios.testing.runtime import LocalRuntime, RunHandle, RunResult
from aios.tools import NativeProvider, tool

SHOP = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: shop, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: refund}]
  agents: [clerk]
  workflows: [refund]
  policies: [refunds]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: clerk, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "edits_shop:refund"}
  capabilities: [{name: shop.refund}]
  tools: {allow: ["shop.>"]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: refund, version: 1.0.0}
spec:
  steps:
%s
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: refunds, version: 1.0.0}
spec:
  subject: {app: shop}
  allow:
    - {action: tool.invoke, resource: "shop.>"}
  deny:
    - {action: tool.invoke, resource: shop.refund.create, when: {input: {amount: {gt: 10000}}}}
  requireApproval:
    - action: tool.invoke
      resource: shop.refund.create
      when: {input: {amount: {gt: 100}}}
      approvers: ["role:lead"]
    - action: tool.invoke
      resource: shop.refund.create
      when: {input: {amount: {gt: 1000}}}
      approvers: ["role:cfo"]
"""
REFUND = "    - {id: refund, type: task, capability: shop.refund, input: {amount: 120}}"
LEAD = Identity("user:lead@acme", "acme", frozenset({"member", "lead"}))


async def _refund(ctx: Context, task: dict[str, Any]) -> Any:
    tool_name = task.get("tool", "shop.refund.create")
    return await ctx.tools.invoke(tool_name, order="1001", amount=task["amount"])


Shop = tuple[LocalRuntime, list[float]]


@pytest.fixture
async def shop(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Callable[..., Awaitable[Shop]]]:
    module = ModuleType("edits_shop")
    module.refund = _refund  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edits_shop", module)
    running: list[LocalRuntime] = []

    async def make(steps: str = REFUND, broken_preview: bool = False) -> Shop:
        paid: list[float] = []
        runtime = LocalRuntime(tenant="acme")
        await runtime.__aenter__()
        running.append(runtime)

        @tool("shop.refund.create", side_effect=SideEffect.FINANCIAL, trusted=True)
        async def refund(order: str, amount: float) -> dict[str, Any]:
            paid.append(amount)
            return {"refunded": amount, "order": order}

        @refund.previewer
        async def _what_it_would_do(order: str, amount: float) -> str:
            if broken_preview:
                raise RuntimeError("the till can't say")
            return f"would refund ${amount:.2f} for order {order}"

        await runtime.runtime.register_tool_provider(NativeProvider("shop", [refund]))
        await runtime.runtime.apply(load_manifests(SHOP % steps), tenant="acme")
        return runtime, paid

    yield make
    for runtime in running:
        await runtime.__aexit__(None, None, None)


async def asked(runtime: LocalRuntime) -> tuple[RunHandle, Approval]:
    handle = await runtime.chat("shop", "refund it")
    event = await handle.wait_for_event("approval.requested", timeout=10)
    data = event.data if isinstance(event.data, dict) else {}
    async with asyncio.timeout(10):
        while True:
            found = await runtime.runtime.human.get("acme", str(data["approval"]))
            if found is not None:
                return handle, found
            await asyncio.sleep(0.02)


async def published(runtime: LocalRuntime, run: str, type: str) -> list[dict[str, Any]]:
    scope = Scope("acme", "shop")
    events = [s.event async for s in runtime.runtime.bus.replay(scope, type, run_id=run)]
    return [e.data for e in events if isinstance(e.data, dict)]


async def test_an_approver_changes_the_amount_and_that_is_what_runs(shop: Any) -> None:
    runtime, paid = await shop()
    handle, approval = await asked(runtime)
    granted = await runtime.runtime.human.decide(
        LEAD, approval.id, approve=True, comment="half, it arrived late", edits={"amount": 60}
    )
    assert granted.status == "granted"
    result: RunResult = await handle.wait(timeout=10)
    assert result.status == "completed"
    assert paid == [60]
    [modified] = await published(runtime, handle.run_id, "approval.modified")
    assert modified["edits"] == {"amount": 60}
    assert modified["payload"] == {"order": "1001", "amount": 60}
    [completed] = await published(runtime, handle.run_id, "tool.call.completed")
    assert completed["arguments"] == {"order": "1001", "amount": 60}  # what was really called


async def test_a_change_that_needs_more_approval_is_refused(shop: Any) -> None:
    """An approver for refunds over $100 can't approve one over $1000 by editing it."""
    runtime, paid = await shop()
    handle, approval = await asked(runtime)
    await runtime.runtime.human.decide(LEAD, approval.id, approve=True, edits={"amount": 5000})
    result = await handle.wait(timeout=10)
    assert result.status == "failed"
    assert result.error is not None
    assert "needs another approval" in str(result.error.get("message"))
    assert paid == []


async def test_a_change_that_policy_forbids_is_refused(shop: Any) -> None:
    runtime, paid = await shop()
    handle, approval = await asked(runtime)
    await runtime.runtime.human.decide(LEAD, approval.id, approve=True, edits={"amount": 20000})
    result = await handle.wait(timeout=10)
    assert result.status == "failed"
    assert paid == []


async def test_a_change_within_the_same_rules_runs(shop: Any) -> None:
    runtime, paid = await shop()
    handle, approval = await asked(runtime)
    await runtime.runtime.human.decide(LEAD, approval.id, approve=True, edits={"amount": 150})
    assert (await handle.wait(timeout=10)).status == "completed"
    assert paid == [150]


async def test_a_change_of_the_wrong_type_is_refused_before_anything_runs(shop: Any) -> None:
    """No policy looks at the order: only the tool's schema stands in the way."""
    runtime, paid = await shop()
    handle, approval = await asked(runtime)
    await runtime.runtime.human.decide(LEAD, approval.id, approve=True, edits={"order": 1001})
    result = await handle.wait(timeout=10)
    assert result.status == "failed"
    assert result.error is not None
    # refused by AIOS as an answer, not left to the tool to choke on
    assert (result.error.get("code"), "as changed" in str(result.error.get("message"))) == (
        "policy.denied",
        True,
    )
    assert paid == []


async def test_what_can_be_changed(shop: Any) -> None:
    runtime, _ = await shop()
    human = runtime.runtime.human
    _, approval = await asked(runtime)
    with pytest.raises(ValueError, match="no account"):
        await human.decide(LEAD, approval.id, approve=True, edits={"account": "mine"})
    with pytest.raises(ApprovalForbiddenError, match="only a yes"):
        await human.decide(LEAD, approval.id, approve=False, edits={"amount": 1})
    assert (await human.get("acme", approval.id)) is not None  # still open, nothing decided


async def test_an_approver_sees_what_the_tool_says_it_would_do(shop: Any) -> None:
    runtime, _ = await shop()
    _, approval = await asked(runtime)
    assert approval.details.preview == "would refund $120.00 for order 1001"
    assert approval.view()["preview"] == "would refund $120.00 for order 1001"


async def test_a_preview_that_fails_still_asks(shop: Any) -> None:
    runtime, _ = await shop(broken_preview=True)
    _, approval = await asked(runtime)
    assert approval.details.preview is None


async def test_an_edited_approval_step_passes_the_change_on(shop: Any) -> None:
    steps = (
        '    - {id: check, type: approval, approvers: ["role:lead"]}\n'
        "    - {id: done, type: task, capability: shop.refund, needs: [check],"
        " input: {amount: 50}}"
    )
    runtime, _ = await shop(steps)
    handle, approval = await asked(runtime)
    assert approval.details.payload == {"message": "refund it"}
    await runtime.runtime.human.decide(
        LEAD, approval.id, approve=True, edits={"message": "check it twice"}
    )
    await handle.wait(timeout=10)
    completed = await published(runtime, handle.run_id, "step.completed")
    check = next(c for c in completed if c["step"] == "check")
    assert check["output"]["payload"] == {"message": "check it twice"}

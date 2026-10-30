"""Acceptance tests for the AI Support Desk reference app (apps/ai-support-desk, stories S1-S7).

Written in Phase 0 against the in-process test runtime (``aios.testing.runtime``, M1) and the
Phase 2 security, memory and approval features. Expected to fail until M2 lands them.
"""
# pyright: basic, reportIndexIssue=false, reportArgumentType=false, reportCallIssue=false, reportOptionalSubscript=false, reportOptionalMemberAccess=false, reportOperatorIssue=false, reportAttributeAccessIssue=false
# The runtime APIs these stories need (memory, approvals, audit) arrive in Phase 2.

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(
        reason="WP 2.1-2.7: memory, security kernel, approvals and the app's agents arrive in M2",
    ),
]

APP = "apps/ai-support-desk"
CASSETTES = "tests/cassettes/ai-support-desk"
CONFIG = {"refundApprovalThresholdUSD": 50, "approverRole": "support-lead"}


async def test_answers_from_the_knowledge_base_with_citations() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, config=CONFIG) as runtime:
        await runtime.ingest_knowledge("ai-support-desk", "tests/fixtures/kb/")
        result = await (await runtime.chat("ai-support-desk", "How do I reset my password?")).wait()

    assert result.status == "completed"
    assert "kb/password-reset.md" in result.output["citations"]


async def test_refund_above_threshold_needs_approval_and_executes_once() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, config=CONFIG) as runtime:
        run = await runtime.chat("ai-support-desk", "Refund order 1001 ($120), item arrived broken")
        approval = await run.wait_for_event("approval.requested")
        assert approval.data["approvers"] == ["role:support-lead"]
        await runtime.approve(approval.data["approval"], by="user:lead@acme")
        result = await run.wait()

    assert result.status == "completed"
    assert runtime.tool_calls("stripe.refund.create") == 1


async def test_refund_below_threshold_executes_without_approval() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, config=CONFIG) as runtime:
        result = await (await runtime.chat("ai-support-desk", "Refund order 1002 ($20)")).wait()

    assert not [e for e in result.events if e.type == "approval.requested"]
    assert runtime.tool_calls("stripe.refund.create") == 1


async def test_poisoned_article_cannot_trigger_a_refund() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, config=CONFIG) as runtime:
        await runtime.ingest_knowledge("ai-support-desk", "tests/fixtures/kb-poisoned/")
        result = await (
            await runtime.chat("ai-support-desk", "What is your returns policy?")
        ).wait()

    types = {e.type for e in result.events}
    assert runtime.tool_calls("stripe.refund.create") == 0
    assert "approval.requested" in types or "policy.decision.denied" in types


async def test_tenants_cannot_see_each_others_tickets() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, tenants=["acme", "globex"]) as rt:
        await (await rt.chat("ai-support-desk", "Refund order 1003", tenant="acme")).wait()
        globex = rt.client(tenant="globex")
        assert await globex.runs.list() == []
        assert await globex.memory.search("order 1003", scopes=["app", "user"]) == []
        assert [e async for e in globex.events.replay()] == []


async def test_remembers_preferences_and_forgets_on_request() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, config=CONFIG) as runtime:
        alice = runtime.client(user="user:alice@acme")
        await (await alice.chat("ai-support-desk", "Please email me, never call.")).wait()
        later = await (await alice.chat("ai-support-desk", "How will you contact me?")).wait()
        assert "email" in later.output["reply"].lower()
        await runtime.memory.forget(subject="user:alice@acme")
        assert await runtime.memory.search("contact preference", scopes=["user"]) == []


async def test_audit_chain_verifies() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, config=CONFIG) as runtime:
        await (await runtime.chat("ai-support-desk", "Refund order 1004 ($20)")).wait()
        assert (await runtime.audit.verify()).ok
        await runtime.audit.tamper_for_test(index=0)
        assert not (await runtime.audit.verify()).ok

"""The Cedar engine (ADR-007) against the requirements §7.9 example and the Support Desk policy."""

import json
from pathlib import Path
from typing import Any

import pytest

from aios.core.errors import InvalidPolicyError
from aios.core.manifests import PolicyManifest, load_manifests
from aios.core.policy import (
    Actor,
    Effect,
    PolicyAction,
    PolicyDecision,
    PolicyRequest,
    Resource,
    SideEffect,
)
from aios.security.cedar import CedarDecisionPoint, compile_policies, schema_text

ROOT = Path(__file__).resolve().parents[3]
DESK = ROOT / "apps" / "ai-support-desk"

RESEARCH_POLICY = """
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: research-agent-policy, version: 1.0.0}
spec:
  subject: {agent: research-agent}
  allow:
    - {action: tool.invoke, resource: "search.>"}
    - {action: memory.read, scopes: [session, app, tenant]}
  deny:
    - {action: tool.invoke, resource: "filesystem.*.delete"}
  requireApproval:
    - action: tool.invoke
      when: {sideEffect: [external_comm, financial]}
      approvers: ["role:team-lead"]
"""


def _policies(text: str, config: dict[str, Any] | None = None) -> list[PolicyManifest]:
    manifests = load_manifests(text, config=config)
    assert all(isinstance(m, PolicyManifest) for m in manifests)
    return [m for m in manifests if isinstance(m, PolicyManifest)]


def _desk_config() -> dict[str, Any]:
    schema = json.loads((DESK / "config.schema.json").read_text(encoding="utf-8"))
    return {name: spec["default"] for name, spec in schema["properties"].items()}


def _engine(text: str, config: dict[str, Any] | None = None) -> CedarDecisionPoint:
    return CedarDecisionPoint.from_manifests(_policies(text, config), tenant="acme")


@pytest.fixture(scope="module")
def research() -> CedarDecisionPoint:
    return _engine(RESEARCH_POLICY)


@pytest.fixture(scope="module")
def desk() -> CedarDecisionPoint:
    text = (DESK / "policies" / "support-desk-policy.yaml").read_text(encoding="utf-8")
    return _engine(text, _desk_config())


def _tool(
    engine: CedarDecisionPoint,
    tool: str,
    side_effect: SideEffect = SideEffect.READ,
    *,
    agent: str = "research-agent",
    app: str = "ai-researcher",
    **fields: Any,
) -> PolicyDecision:
    request = PolicyRequest(
        tenant=fields.pop("tenant", "acme"),
        app=app,
        actor=Actor(f"agent:{agent}@acme"),
        action=PolicyAction.TOOL_INVOKE,
        resource=Resource.tool(tool, side_effect),
        **fields,
    )
    return engine.decide_now(request)


def _refund(engine: CedarDecisionPoint, amount: Any, **fields: Any) -> PolicyDecision:
    return _tool(
        engine,
        "stripe.refund.create",
        SideEffect.FINANCIAL,
        agent="refund-agent",
        app="ai-support-desk",
        input={"amountUSD": amount},
        **fields,
    )


# --------------------------------------------------------------------------- §7.9 example


def test_allows_matching_tools(research: CedarDecisionPoint) -> None:
    decision = _tool(research, "search.web.query")
    assert decision.effect is Effect.ALLOW
    assert decision.policies == ("research-agent-policy/allow/0",)


def test_deny_rules_and_default_deny(research: CedarDecisionPoint) -> None:
    denied = _tool(research, "filesystem.file.delete", SideEffect.DESTRUCTIVE)
    assert (denied.effect, denied.policies) == (Effect.DENY, ("research-agent-policy/deny/0",))
    unlisted = _tool(research, "filesystem.file.read")
    assert (unlisted.effect, unlisted.policies) == (Effect.DENY, ())
    assert "default deny" in unlisted.reason
    assert _tool(research, "search.web.query", agent="writer-agent").effect is Effect.DENY


def test_side_effects_require_approval_until_approved(research: CedarDecisionPoint) -> None:
    pending = _tool(research, "search.alert.send", SideEffect.EXTERNAL_COMM)
    assert pending.effect is Effect.REQUIRE_APPROVAL
    assert pending.approvers == ("role:team-lead",)
    assert pending.policies == ("research-agent-policy/approval/0",)
    approved = _tool(research, "search.alert.send", SideEffect.EXTERNAL_COMM, approved=True)
    assert approved.effect is Effect.ALLOW


def test_approval_never_grants_what_no_rule_allows(research: CedarDecisionPoint) -> None:
    decision = _tool(research, "stripe.refund.create", SideEffect.FINANCIAL)
    assert decision.effect is Effect.DENY


@pytest.mark.parametrize(("scope", "effect"), [("session", Effect.ALLOW), ("user", Effect.DENY)])
def test_memory_scopes(research: CedarDecisionPoint, scope: str, effect: Effect) -> None:
    for action, expected in (
        (PolicyAction.MEMORY_READ, effect),
        (PolicyAction.MEMORY_WRITE, Effect.DENY),
    ):
        request = PolicyRequest(
            tenant="acme",
            app="ai-researcher",
            actor=Actor("agent:research-agent@acme"),
            action=action,
            resource=Resource.memory_scope(scope),
        )
        assert research.decide_now(request).effect is expected


def test_compiled_cedar_uses_exact_token_checks(research: CedarDecisionPoint) -> None:
    text = research.compiled.text
    assert 'principal == Agent::"research-agent"' in text
    assert 'resource.provider == "filesystem" && resource.verb == "delete"' in text
    assert "unless { context.approved }" in text
    assert " like " not in text


# --------------------------------------------------------------------------- Support Desk


def test_refunds_below_the_threshold_are_allowed(desk: CedarDecisionPoint) -> None:
    decision = _refund(desk, 20)
    assert decision.effect is Effect.ALLOW
    assert decision.policies == ("support-desk-policy/allow/1",)


def test_refunds_above_the_threshold_need_the_configured_approver(desk: CedarDecisionPoint) -> None:
    pending = _refund(desk, 120.5)
    assert pending.effect is Effect.REQUIRE_APPROVAL
    assert pending.approvers == ("role:support-lead",)
    assert _refund(desk, 120.5, approved=True).effect is Effect.ALLOW


def test_tainted_context_escalates_financial_and_external_tools(desk: CedarDecisionPoint) -> None:
    # The poisoned-article scenario (story S4): a small refund is still escalated.
    assert _refund(desk, 20, tainted=True).effect is Effect.REQUIRE_APPROVAL
    reply = {"agent": "reply-agent", "app": "ai-support-desk"}
    assert _tool(desk, "zendesk.ticket.reply", SideEffect.EXTERNAL_COMM, **reply).allowed
    tainted = _tool(desk, "zendesk.ticket.reply", SideEffect.EXTERNAL_COMM, tainted=True, **reply)
    assert tainted.effect is Effect.REQUIRE_APPROVAL


def test_app_subjects_cover_every_agent_of_the_app(desk: CedarDecisionPoint) -> None:
    assert _tool(desk, "zendesk.ticket.get", agent="kb-agent", app="ai-support-desk").allowed
    assert not _tool(desk, "zendesk.ticket.get", agent="kb-agent", app="ai-researcher").allowed


@pytest.mark.parametrize("amount", ["500", 10.12345, True, None, float("inf"), 1e20])
def test_inputs_that_cannot_be_encoded_are_denied(desk: CedarDecisionPoint, amount: Any) -> None:
    decision = _refund(desk, amount)
    assert decision.effect is Effect.DENY
    assert "input.amountUSD" in decision.reason


def test_a_missing_input_field_does_not_match_the_condition(desk: CedarDecisionPoint) -> None:
    decision = _tool(
        desk,
        "stripe.refund.create",
        SideEffect.FINANCIAL,
        agent="refund-agent",
        app="ai-support-desk",
    )
    assert decision.effect is Effect.ALLOW


def test_other_tenants_are_denied(desk: CedarDecisionPoint) -> None:
    decision = _refund(desk, 20, tenant="globex")
    assert decision.effect is Effect.DENY


# --------------------------------------------------------------------------- semantics


def test_forbid_wins_over_permit() -> None:
    engine = _engine(
        """
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: stripe, version: 1.0.0}
spec:
  subject: {agent: research-agent}
  allow: [{action: tool.invoke, resource: "stripe.>"}]
  deny: [{action: tool.invoke, resource: "stripe.customer.>"}]
"""
    )
    assert _tool(engine, "stripe.refund.create", SideEffect.FINANCIAL).allowed
    denied = _tool(engine, "stripe.customer.delete", SideEffect.DESTRUCTIVE)
    assert (denied.effect, denied.policies) == (Effect.DENY, ("stripe/deny/0",))


@pytest.mark.parametrize(
    ("tool", "allowed"),
    [
        ("filesystem.file.read", True),
        ("filesystem.dir.read", True),
        ("filesystem.file.write", False),
    ],
)
def test_single_token_wildcards(tool: str, allowed: bool) -> None:
    engine = _engine(
        """
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: fs, version: 1.0.0}
spec:
  subject: {agent: research-agent}
  allow: [{action: tool.invoke, resource: "filesystem.*.read"}]
"""
    )
    assert _tool(engine, tool).allowed is allowed


DELEGATION = """
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: agent, version: 1.0.0}
spec:
  subject: {agent: kb-agent}
  allow: [{action: tool.invoke, resource: "zendesk.>"}]
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: support-role, version: 1.0.0}
spec:
  subject: {role: support}
  allow: [{action: tool.invoke, resource: "zendesk.ticket.>"}]
"""


@pytest.mark.parametrize(
    ("user", "tool", "effect"),
    [
        (Actor("user:alice@acme", frozenset({"support"})), "zendesk.ticket.get", Effect.ALLOW),
        (Actor("user:bob@acme"), "zendesk.ticket.get", Effect.DENY),
        (Actor("user:alice@acme", frozenset({"support"})), "zendesk.user.delete", Effect.DENY),
    ],
)
def test_delegation_is_the_intersection(user: Actor, tool: str, effect: Effect) -> None:
    engine = _engine(DELEGATION)
    decision = _tool(engine, tool, agent="kb-agent", app="ai-support-desk", on_behalf_of=user)
    assert decision.effect is effect


def test_requests_that_cannot_be_evaluated_are_denied(research: CedarDecisionPoint) -> None:
    unknown_kind = PolicyRequest(
        tenant="acme",
        app="ai-researcher",
        actor=Actor("robot:r2@acme"),
        action=PolicyAction.TOOL_INVOKE,
        resource=Resource.tool("search.web.query", SideEffect.READ),
    )
    assert research.decide_now(unknown_kind).effect is Effect.DENY
    assert _tool(research, "search.query").effect is Effect.DENY  # not a 3-token tool name
    no_side_effect = PolicyRequest(
        tenant="acme",
        app="ai-researcher",
        actor=Actor("agent:research-agent@acme"),
        action=PolicyAction.TOOL_INVOKE,
        resource=Resource("Tool", "search.web.query"),
    )
    assert research.decide_now(no_side_effect).effect is Effect.DENY
    wrong_resource = PolicyRequest(
        tenant="acme",
        app="ai-researcher",
        actor=Actor("agent:research-agent@acme"),
        action=PolicyAction.MEMORY_READ,
        resource=Resource.tool("search.web.query", SideEffect.READ),
    )
    decision = research.decide_now(wrong_resource)
    assert decision.effect is Effect.DENY
    assert "evaluation failed" in decision.reason


async def test_decide_is_the_async_port(research: CedarDecisionPoint) -> None:
    request = PolicyRequest(
        tenant="acme",
        app="ai-researcher",
        actor=Actor("agent:research-agent@acme"),
        action=PolicyAction.TOOL_INVOKE,
        resource=Resource.tool("search.web.query", SideEffect.READ),
    )
    assert (await research.decide(request)).allowed


# --------------------------------------------------------------------------- compiling


def _rule_policy(when: str) -> str:
    return f"""
apiVersion: aios/v1alpha1
kind: Policy
metadata: {{name: p, version: 1.0.0}}
spec:
  subject: {{agent: a}}
  allow:
    - {{action: tool.invoke, when: {when}}}
    - {{action: tool.invoke, when: {{input: {{amountUSD: {{eq: 5}}}}}}}}
"""


@pytest.mark.parametrize(
    ("when", "message"),
    [
        ("{input: {amountUSD: {gt: 0.00001}}}", "4 decimal places"),
        ("{input: {amountUSD: {eq: gold}}}", "compared as decimal here and as String elsewhere"),
    ],
)
def test_compile_errors(when: str, message: str) -> None:
    with pytest.raises(InvalidPolicyError, match=message):
        compile_policies(_policies(_rule_policy(when)))


def test_schema_lists_the_referenced_input_fields() -> None:
    compiled = compile_policies(_policies(_rule_policy("{input: {note: {eq: hi}}}")))
    assert dict(compiled.input_fields) == {"amountUSD": "decimal", "note": "String"}
    assert "type ToolInput = { amountUSD?: decimal, note?: String };" in compiled.schema
    assert "type ToolInput = {};" in schema_text()


def test_string_and_boolean_input_conditions() -> None:
    engine = _engine(
        """
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: tiers, version: 1.0.0}
spec:
  subject: {agent: research-agent}
  allow:
    - {action: tool.invoke, when: {input: {tier: {eq: gold}, urgent: {eq: true}}}}
"""
    )
    assert _tool(engine, "crm.case.create", input={"tier": "gold", "urgent": True}).allowed
    assert not _tool(engine, "crm.case.create", input={"tier": "silver", "urgent": True}).allowed
    wrong_string = _tool(engine, "crm.case.create", input={"tier": 5, "urgent": True})
    assert "input.tier must be a string" in wrong_string.reason
    wrong_bool = _tool(engine, "crm.case.create", input={"tier": "gold", "urgent": "yes"})
    assert "input.urgent must be a bool" in wrong_bool.reason


def test_an_app_can_act_as_its_own_principal(desk: CedarDecisionPoint) -> None:
    request = PolicyRequest(
        tenant="acme",
        app="ai-support-desk",
        actor=Actor("app:ai-support-desk@acme"),
        action=PolicyAction.TOOL_INVOKE,
        resource=Resource.tool("zendesk.ticket.get", SideEffect.READ),
    )
    assert desk.decide_now(request).allowed


def test_policy_strings_reject_control_characters() -> None:
    with pytest.raises(InvalidPolicyError, match="control characters"):
        # YAML turns the "\t" escape into a tab character.
        compile_policies(_policies(_rule_policy('{input: {note: {eq: "a\\tb"}}}')))

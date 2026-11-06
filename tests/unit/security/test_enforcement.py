"""The enforcer: ceilings, delegation, the kill switch and the built-in roles (FR-SEC-03 to 13)."""

from datetime import UTC, datetime, timedelta

import pytest

from aios.core.identity import Suspendable, Suspension
from aios.core.manifests import PolicyManifest, ToolAccess, load_manifests
from aios.core.policy import (
    CONTROL_ACTIONS,
    Actor,
    Effect,
    PolicyAction,
    PolicyDecision,
    PolicyRequest,
    Resource,
    SideEffect,
)
from aios.security.cedar import CedarDecisionPoint
from aios.security.enforcement import Enforcer, builtin_roles
from aios.security.store import InMemorySecurityStore

TENANT, APP = "acme", "desk"
TRIAGE = Actor("agent:triage-agent@acme")
REFUND = Actor("agent:refund-agent@acme")
ALICE = "user:alice@acme"

APP_WIDE = """
apiVersion: aios/v1alpha1
kind: Policy
metadata: { name: desk-policy, version: 1.0.0 }
spec:
  subject: { app: desk }
  allow:
    - action: tool.invoke
      resource: "stripe.>"
  deny:
    - action: tool.invoke
      resource: "stripe.customer.>"
  requireApproval:
    - action: tool.invoke
      resource: "stripe.refund.create"
      when: { input: { amountUSD: { gt: 50 } } }
      approvers: ["role:support-lead"]
"""


@pytest.fixture
def store() -> InMemorySecurityStore:
    return InMemorySecurityStore()


@pytest.fixture
def enforcer(store: InMemorySecurityStore) -> Enforcer:
    enforcer = Enforcer(store)
    enforcer.install(
        TENANT,
        APP,
        policies=load_manifests(APP_WIDE),
        agents={
            "triage-agent": ToolAccess(allow=["zendesk.ticket.get"]),
            "refund-agent": ToolAccess(
                allow=["zendesk.ticket.get", "stripe.refund.create", "stripe.customer.get"],
                deny=["zendesk.ticket.delete"],
            ),
            "idle-agent": ToolAccess(),
        },
    )
    return enforcer


def _tool(
    actor: Actor,
    name: str,
    side_effect: SideEffect = SideEffect.WRITE,
    **fields: object,
) -> PolicyRequest:
    return PolicyRequest(
        tenant=TENANT,
        app=APP,
        actor=actor,
        action=PolicyAction.TOOL_INVOKE,
        resource=Resource.tool(name, side_effect),
        **fields,  # type: ignore[arg-type]
    )


def _control(action: PolicyAction, *roles: str, kind: str = "user") -> PolicyRequest:
    actor = Actor(f"{kind}:someone@acme", frozenset(roles))
    return PolicyRequest(
        tenant=TENANT, app="aios", actor=actor, action=action, resource=Resource.tenant(TENANT)
    )


def _suspension(kind: Suspendable, name: str) -> Suspension:
    return Suspension(TENANT, kind, name, "incident", "user:ops@acme", datetime.now(UTC))


# --------------------------------------------------------------------------- ceilings


async def test_a_manifest_grants_the_tools_it_lists(enforcer: Enforcer) -> None:
    decision = await enforcer.decide(_tool(TRIAGE, "zendesk.ticket.get", SideEffect.READ))
    assert decision.effect is Effect.ALLOW


async def test_a_broad_app_rule_can_not_hand_an_agent_more_than_its_manifest(
    enforcer: Enforcer,
) -> None:
    """Without the ceiling, the App-wide ``stripe.>`` would give the triage agent refunds."""
    decision = await enforcer.decide(_tool(TRIAGE, "stripe.refund.create"))
    assert decision.effect is Effect.DENY
    assert "triage-agent/ceiling/0" in decision.policies


async def test_an_agent_with_no_tools_can_use_no_tools(enforcer: Enforcer) -> None:
    idle = Actor("agent:idle-agent@acme")
    assert (await enforcer.decide(_tool(idle, "stripe.refund.create"))).effect is Effect.DENY


async def test_an_unknown_agent_gets_nothing(enforcer: Enforcer) -> None:
    stranger = Actor("agent:stranger@acme")
    decision = await enforcer.decide(_tool(stranger, "zendesk.ticket.get"))
    assert decision.effect is Effect.DENY


async def test_a_manifest_deny_wins(enforcer: Enforcer) -> None:
    assert (await enforcer.decide(_tool(REFUND, "zendesk.ticket.delete"))).effect is Effect.DENY


async def test_an_app_deny_overrides_a_manifest_allow(enforcer: Enforcer) -> None:
    assert (await enforcer.decide(_tool(REFUND, "stripe.customer.get"))).effect is Effect.DENY


async def test_wildcard_ceilings_keep_topic_grammar(store: InMemorySecurityStore) -> None:
    enforcer = Enforcer(store)
    enforcer.install(TENANT, APP, policies=(), agents={"a": ToolAccess(allow=["crm.*.get"])})
    actor = Actor("agent:a@acme")
    assert (await enforcer.decide(_tool(actor, "crm.contact.get"))).allowed
    assert not (await enforcer.decide(_tool(actor, "crm.contact.delete"))).allowed


async def test_a_ceiling_of_everything_still_needs_no_more(store: InMemorySecurityStore) -> None:
    enforcer = Enforcer(store)
    enforcer.install(TENANT, APP, policies=(), agents={"a": ToolAccess(allow=[">"])})
    assert (await enforcer.decide(_tool(Actor("agent:a@acme"), "any.tool.at-all"))).allowed


# --------------------------------------------------------------------------- approvals and taint


async def test_app_policy_conditions_still_apply_under_the_ceiling(enforcer: Enforcer) -> None:
    small = _tool(REFUND, "stripe.refund.create", input={"amountUSD": 20})
    large = _tool(REFUND, "stripe.refund.create", input={"amountUSD": 120})
    assert (await enforcer.decide(small)).effect is Effect.ALLOW
    decision = await enforcer.decide(large)
    assert decision.effect is Effect.REQUIRE_APPROVAL
    assert decision.approvers == ("role:support-lead",)


@pytest.mark.parametrize(
    ("side_effect", "effect"),
    [
        (SideEffect.READ, Effect.ALLOW),
        (SideEffect.WRITE, Effect.ALLOW),
        (SideEffect.DESTRUCTIVE, Effect.REQUIRE_APPROVAL),
        (SideEffect.EXTERNAL_COMM, Effect.REQUIRE_APPROVAL),
        (SideEffect.FINANCIAL, Effect.REQUIRE_APPROVAL),
        (SideEffect.CODE_EXEC, Effect.REQUIRE_APPROVAL),
    ],
)
async def test_taint_escalates_every_risky_class_for_every_app(
    store: InMemorySecurityStore, side_effect: SideEffect, effect: Effect
) -> None:
    enforcer = Enforcer(store)
    enforcer.install(TENANT, APP, policies=(), agents={"a": ToolAccess(allow=[">"])})
    request = _tool(Actor("agent:a@acme"), "crm.thing.do", side_effect, tainted=True)
    assert (await enforcer.decide(request)).effect is effect


# --------------------------------------------------------------------------- delegation


@pytest.mark.parametrize(
    ("roles", "effect"),
    [
        ({"member"}, Effect.ALLOW),
        ({"operator"}, Effect.ALLOW),
        ({"tenant-admin"}, Effect.ALLOW),
        ({"auditor"}, Effect.DENY),
        ({"support-lead"}, Effect.DENY),
        (set[str](), Effect.DENY),
    ],
)
async def test_an_agent_can_not_do_more_than_the_user_it_acts_for(
    enforcer: Enforcer, store: InMemorySecurityStore, roles: set[str], effect: Effect
) -> None:
    """FR-SEC-05: a low-privilege user can't drive a high-privilege agent."""
    await store.set_roles(TENANT, ALICE, frozenset(roles))
    request = _tool(REFUND, "zendesk.ticket.get", SideEffect.READ, on_behalf_of=Actor(ALICE))
    assert (await enforcer.decide(request)).effect is effect


async def test_a_user_can_not_lift_an_agent_above_its_manifest(
    enforcer: Enforcer, store: InMemorySecurityStore
) -> None:
    await store.set_roles(TENANT, ALICE, frozenset({"tenant-admin"}))
    request = _tool(TRIAGE, "stripe.refund.create", on_behalf_of=Actor(ALICE))
    assert (await enforcer.decide(request)).effect is Effect.DENY


async def test_a_role_taken_away_applies_to_a_run_already_under_way(
    enforcer: Enforcer, store: InMemorySecurityStore
) -> None:
    await store.set_roles(TENANT, ALICE, frozenset({"member"}))
    request = _tool(REFUND, "zendesk.ticket.get", SideEffect.READ, on_behalf_of=Actor(ALICE))
    assert (await enforcer.decide(request)).allowed
    await store.set_roles(TENANT, ALICE, frozenset())
    assert not (await enforcer.decide(request)).allowed


async def test_an_unknown_delegate_has_no_permissions(enforcer: Enforcer) -> None:
    ghost = _tool(REFUND, "zendesk.ticket.get", on_behalf_of=Actor("user:ghost@acme"))
    assert not (await enforcer.decide(ghost)).allowed


async def test_roles_in_a_request_are_replaced_by_the_directory(enforcer: Enforcer) -> None:
    """A role claimed in the event or request means nothing: the directory answers."""
    claimed = Actor(ALICE, frozenset({"tenant-admin"}))
    request = _tool(REFUND, "zendesk.ticket.get", on_behalf_of=claimed)
    assert not (await enforcer.decide(request)).allowed


async def test_a_service_can_be_a_delegate_too(
    enforcer: Enforcer, store: InMemorySecurityStore
) -> None:
    service = "service:ci@acme"
    await store.set_roles(TENANT, service, frozenset({"member"}))
    request = _tool(REFUND, "zendesk.ticket.get", SideEffect.READ, on_behalf_of=Actor(service))
    assert (await enforcer.decide(request)).allowed


# --------------------------------------------------------------------------- the kill switch


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        (Suspendable.TENANT, TENANT),
        (Suspendable.APP, APP),
        (Suspendable.AGENT, "refund-agent"),
    ],
)
async def test_the_kill_switch_outranks_every_allow(
    enforcer: Enforcer, store: InMemorySecurityStore, kind: Suspendable, name: str
) -> None:
    request = _tool(REFUND, "zendesk.ticket.get", SideEffect.READ)
    assert (await enforcer.decide(request)).allowed
    await store.suspend(_suspension(kind, name))
    decision = await enforcer.decide(request)
    assert decision.effect is Effect.DENY
    assert "incident" in decision.reason
    await store.lift(TENANT, kind, name)
    assert (await enforcer.decide(request)).allowed


async def test_suspending_one_agent_leaves_the_others_working(
    enforcer: Enforcer, store: InMemorySecurityStore
) -> None:
    await store.suspend(_suspension(Suspendable.AGENT, "refund-agent"))
    assert (await enforcer.decide(_tool(TRIAGE, "zendesk.ticket.get", SideEffect.READ))).allowed


async def test_suspending_a_tenant_stops_control_actions_too(
    enforcer: Enforcer, store: InMemorySecurityStore
) -> None:
    await store.suspend(_suspension(Suspendable.TENANT, TENANT))
    request = _control(PolicyAction.RUN_READ, "tenant-admin")
    assert not (await enforcer.decide(request)).allowed


@pytest.mark.parametrize("action", [PolicyAction.TASK_DISPATCH, PolicyAction.MODEL_ROUTE])
async def test_agents_may_work_until_they_are_suspended(
    enforcer: Enforcer, store: InMemorySecurityStore, action: PolicyAction
) -> None:
    request = PolicyRequest(
        tenant=TENANT, app=APP, actor=REFUND, action=action, resource=Resource.tenant(TENANT)
    )
    assert (await enforcer.decide(request)).allowed
    await store.suspend(_suspension(Suspendable.AGENT, "refund-agent"))
    assert not (await enforcer.decide(request)).allowed


# --------------------------------------------------------------------------- what isn't installed


async def test_an_app_that_is_not_installed_has_no_permissions(enforcer: Enforcer) -> None:
    request = PolicyRequest(
        tenant=TENANT,
        app="other",
        actor=REFUND,
        action=PolicyAction.TOOL_INVOKE,
        resource=Resource.tool("zendesk.ticket.get", SideEffect.READ),
    )
    decision = await enforcer.decide(request)
    assert decision.effect is Effect.DENY
    assert "no policies" in decision.reason


async def test_policies_do_not_cross_tenants(enforcer: Enforcer) -> None:
    request = PolicyRequest(
        tenant="globex",
        app=APP,
        actor=REFUND,
        action=PolicyAction.TOOL_INVOKE,
        resource=Resource.tool("zendesk.ticket.get", SideEffect.READ),
    )
    assert not (await enforcer.decide(request)).allowed


async def test_reinstalling_an_app_replaces_its_policies(enforcer: Enforcer) -> None:
    request = _tool(TRIAGE, "zendesk.ticket.get", SideEffect.READ)
    assert (await enforcer.decide(request)).allowed
    enforcer.install(TENANT, APP, policies=(), agents={"triage-agent": ToolAccess()})
    assert not (await enforcer.decide(request)).allowed


# --------------------------------------------------------------------------- roles


GRANTS = {
    "member": {
        PolicyAction.APP_READ,
        PolicyAction.RUN_START,
        PolicyAction.RUN_READ,
        PolicyAction.APPROVAL_READ,
        PolicyAction.APPROVAL_DECIDE,
    },
    "auditor": {
        PolicyAction.APP_READ,
        PolicyAction.APPROVAL_READ,
        PolicyAction.RUN_READ,
        PolicyAction.EVENT_READ,
        PolicyAction.AUDIT_READ,
    },
    "operator": {
        PolicyAction.APP_READ,
        PolicyAction.APPROVAL_READ,
        PolicyAction.APPROVAL_DECIDE,
        PolicyAction.RUN_START,
        PolicyAction.RUN_READ,
        PolicyAction.RUN_CONTROL,
        PolicyAction.EVENT_READ,
        PolicyAction.DLQ_MANAGE,
    },
    "tenant-admin": {
        PolicyAction.APP_READ,
        PolicyAction.APPROVAL_READ,
        PolicyAction.APPROVAL_DECIDE,
        PolicyAction.RUN_START,
        PolicyAction.RUN_READ,
        PolicyAction.RUN_CONTROL,
        PolicyAction.EVENT_READ,
        PolicyAction.DLQ_MANAGE,
        PolicyAction.AUDIT_READ,
        PolicyAction.KEY_MANAGE,
        PolicyAction.MEMORY_MANAGE,
        PolicyAction.SUSPEND,
        PolicyAction.APP_MANAGE,
    },
    "platform-admin": set(CONTROL_ACTIONS),
    "support-lead": set[PolicyAction](),
}


@pytest.mark.parametrize("role", GRANTS)
@pytest.mark.parametrize("action", sorted(CONTROL_ACTIONS))
@pytest.mark.parametrize("kind", ["user", "service"])
async def test_every_role_gets_exactly_what_adr_011_says(
    enforcer: Enforcer, role: str, action: PolicyAction, kind: str
) -> None:
    decision = await enforcer.decide(_control(action, role, kind=kind))
    assert decision.allowed is (action in GRANTS[role])


async def test_no_role_means_no_control_action(enforcer: Enforcer) -> None:
    for action in sorted(CONTROL_ACTIONS):
        assert not (await enforcer.decide(_control(action))).allowed


async def test_an_agent_can_not_use_the_control_plane(enforcer: Enforcer) -> None:
    """A control action names a user or a service; an agent is neither, so Cedar refuses it."""
    request = PolicyRequest(
        tenant=TENANT,
        app=APP,
        actor=Actor("agent:refund-agent@acme", frozenset({"platform-admin"})),
        action=PolicyAction.SUSPEND,
        resource=Resource.tenant(TENANT),
    )
    assert not (await enforcer.decide(request)).allowed


def test_only_platform_admin_can_install_apps() -> None:
    can_install = [
        policy.spec.subject.name
        for policy in builtin_roles()
        if any(rule.action is PolicyAction.APP_INSTALL for rule in policy.spec.allow)
    ]
    assert can_install == ["platform-admin"]


# --------------------------------------------------------------------------- approval timeouts

TIMED = """
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: timed, version: 1.0.0}
spec:
  subject: {app: desk}
  allow:
    - {action: tool.invoke, resource: "pay.refund.create"}
  requireApproval:
    - {action: tool.invoke, resource: "pay.refund.create", approvers: ["role:lead"],
       timeout: 2h, onTimeout: approve}
    - {action: tool.invoke, resource: "pay.>", approvers: ["role:finance"], timeout: 30m,
       onTimeout: %s}
"""


def _decided(on_timeout: str) -> PolicyDecision:
    [policy] = [m for m in load_manifests(TIMED % on_timeout) if isinstance(m, PolicyManifest)]
    point = CedarDecisionPoint.from_manifests([policy], tenant="acme")
    return point.decide_now(
        PolicyRequest(
            tenant="acme",
            app="desk",
            actor=Actor("agent:refund-agent@acme"),
            action=PolicyAction.TOOL_INVOKE,
            resource=Resource.tool("pay.refund.create", SideEffect.FINANCIAL),
        )
    )


def test_the_shortest_time_any_rule_gives_applies() -> None:
    decision = _decided("approve")
    assert decision.effect is Effect.REQUIRE_APPROVAL
    assert decision.timeout == timedelta(minutes=30)


def test_an_action_goes_ahead_unanswered_only_if_every_rule_lets_it() -> None:
    assert _decided("approve").on_timeout == "approve"
    assert _decided("deny").on_timeout == "deny"


def test_approvers_of_every_rule_are_named() -> None:
    assert set(_decided("deny").approvers) == {"role:lead", "role:finance"}


SPLIT = """
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: split-agent, version: 1.0.0}
spec:
  subject: {agent: refund-agent}
  allow:
    - {action: tool.invoke, resource: "pay.refund.create"}
  requireApproval:
    - {action: tool.invoke, resource: "pay.refund.create", approvers: ["role:lead"],
       timeout: 2h, onTimeout: approve}
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: split-user, version: 1.0.0}
spec:
  subject: {role: member}
  allow:
    - {action: tool.invoke, resource: "pay.refund.create"}
  requireApproval:
    - {action: tool.invoke, resource: "pay.refund.create", approvers: ["role:finance"],
       timeout: 30m}
"""


def test_the_person_an_agent_acts_for_counts_too() -> None:
    """Delegation intersects: the stricter of the two limits applies to the approval as well."""
    policies = [m for m in load_manifests(SPLIT) if isinstance(m, PolicyManifest)]
    point = CedarDecisionPoint.from_manifests(policies, tenant="acme")
    decision = point.decide_now(
        PolicyRequest(
            tenant="acme",
            app="desk",
            actor=Actor("agent:refund-agent@acme"),
            action=PolicyAction.TOOL_INVOKE,
            resource=Resource.tool("pay.refund.create", SideEffect.FINANCIAL),
            on_behalf_of=Actor("user:alice@acme", frozenset({"member"})),
        )
    )
    assert decision.effect is Effect.REQUIRE_APPROVAL
    assert (decision.timeout, decision.on_timeout) == (timedelta(minutes=30), "deny")
    assert set(decision.approvers) == {"role:lead", "role:finance"}

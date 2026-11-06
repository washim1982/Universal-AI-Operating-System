"""The policy decision point the runtime uses (ADR-007; FR-SEC-03, 04, 05, 13).

Every governed action reaches :class:`Enforcer`, which adds what a compiled policy set can't know:

* **The kill switch comes first.** A suspended tenant, App or agent is denied before any policy is
  read, so a broad ``allow`` can't outrank it.
* **One policy set per App in each tenant:** the built-in roles, the App's Policy manifests and what
  its agents' manifests declare. Control-plane actions use the built-in roles alone.
* **Delegation.** The delegate's roles are read now, from the directory, so a role taken away also
  applies to runs already under way.

Each agent's manifest both grants its tools and memory scopes and caps the agent at them. Without a
cap, a rule for a whole App would hand ``stripe.refund.create`` to its triage agent too. Anything
that is not installed is denied.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from importlib import resources

from aios.core.apps import Grants
from aios.core.identity import SecurityStore
from aios.core.manifests import (
    AnyManifest,
    MemoryAccess,
    PolicyManifest,
    ToolAccess,
    load_manifests,
)
from aios.core.policy import (
    CONTROL_ACTIONS,
    Actor,
    Effect,
    PolicyAction,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyRequest,
    SideEffect,
)
from aios.security.cedar import CedarDecisionPoint, Ceiling, CompiledPolicies

SHARED_SCOPES = ("agent", "app", "tenant")
"""Memory read by other runs and users."""
TAINT_ESCALATED = (
    SideEffect.DESTRUCTIVE,
    SideEffect.EXTERNAL_COMM,
    SideEffect.FINANCIAL,
    SideEffect.CODE_EXEC,
)
"""While a run's context holds untrusted content, these need an approval (FR-SEC-09)."""
TAINT_APPROVERS = ("role:tenant-admin",)


def builtin_roles() -> list[PolicyManifest]:
    text = resources.files("aios.security").joinpath("defaults/roles.yaml").read_text("utf-8")
    return [m for m in load_manifests(text) if isinstance(m, PolicyManifest)]


def _manifest(name: str, spec: Mapping[str, object]) -> PolicyManifest:
    return PolicyManifest.model_validate(
        {"metadata": {"name": name, "version": "1.0.0"}, "spec": spec}
    )


def _app_defaults(app: str, exempt: Sequence[str] = ()) -> PolicyManifest:
    """What every App gets: its agents may work and call models, and taint escalates.

    Memory has two rules of its own. No agent writes to the operator's global scope. And an agent
    holding untrusted content can't write where others will read it, which is what stops one
    poisoned document from becoming a "fact" for every later run (FR-MEM-05).

    ``exempt`` is what an App's policies declare in ``spec.taint.exempt``: a reply step, say, that
    would otherwise need a person for every message it sends (FR-SEC-09, "configurable per policy").
    """
    return _manifest(
        "app-defaults",
        {
            "subject": {"app": app},
            "allow": [{"action": PolicyAction.TASK_DISPATCH}, {"action": PolicyAction.MODEL_ROUTE}],
            "deny": [
                {"action": PolicyAction.MEMORY_WRITE, "scopes": ["global"]},
                {
                    "action": PolicyAction.MEMORY_WRITE,
                    "scopes": list(SHARED_SCOPES),
                    "when": {"contextTainted": True},
                },
            ],
            "requireApproval": [
                {
                    "action": PolicyAction.TOOL_INVOKE,
                    "when": {
                        "sideEffect": list(TAINT_ESCALATED),
                        "contextTainted": True,
                        "exceptTools": list(exempt),
                    },
                    "approvers": list(TAINT_APPROVERS),
                }
            ],
        },
    )


def consent_policy(app: str, requested: Grants, granted: Grants) -> PolicyManifest:
    """What a tenant admin didn't agree to, refused whatever else says yes (FR-APP-03).

    An App's own policies can't undo this: in Cedar a forbid beats any permit.
    """
    deny: list[dict[str, object]] = [
        {"action": PolicyAction.TOOL_INVOKE, "when": {"exceptTools": list(granted.tools)}}
    ]
    refused = [scope for scope in requested.memory_scopes if scope not in granted.memory_scopes]
    if refused:
        deny.append({"action": PolicyAction.MEMORY_READ, "scopes": refused})
        deny.append({"action": PolicyAction.MEMORY_WRITE, "scopes": refused})
    return _manifest("consent", {"subject": {"app": app}, "deny": deny})


def _agent_policy(agent: str, tools: ToolAccess, memory: MemoryAccess) -> PolicyManifest | None:
    rule = PolicyAction.TOOL_INVOKE
    allow: list[dict[str, object]] = [{"action": rule, "resource": name} for name in tools.allow]
    deny = [{"action": rule, "resource": pattern} for pattern in tools.deny]
    if memory.read:
        allow.append({"action": PolicyAction.MEMORY_READ, "scopes": list(memory.read)})
    if memory.write:
        allow.append({"action": PolicyAction.MEMORY_WRITE, "scopes": list(memory.write)})
    if not allow and not deny:
        return None
    return _manifest(agent, {"subject": {"agent": agent}, "allow": allow, "deny": deny})


class Enforcer(PolicyDecisionPoint):
    def __init__(self, store: SecurityStore) -> None:
        self.store = store
        self._roles = builtin_roles()
        self._control = CedarDecisionPoint.from_manifests(self._roles, tenant=None)
        self._apps: dict[tuple[str, str], CedarDecisionPoint] = {}
        self._sources: dict[tuple[str, str], list[PolicyManifest]] = {}

    def install(
        self,
        tenant: str,
        app: str,
        *,
        policies: Iterable[AnyManifest],
        agents: Mapping[str, ToolAccess],
        memory: Mapping[str, MemoryAccess] | None = None,
    ) -> None:
        """Compile an App's policy set; replaces what the App had before."""
        grants = memory or {}
        nothing = MemoryAccess()
        declared = [
            policy
            for agent, tools in agents.items()
            if (policy := _agent_policy(agent, tools, grants.get(agent, nothing)))
        ]
        owned = [m for m in policies if isinstance(m, PolicyManifest)]
        exempt = [tool for policy in owned for tool in policy.spec.taint.exempt]
        manifests = [*self._roles, _app_defaults(app, exempt), *declared, *owned]
        self._sources[(tenant, app)] = manifests[len(self._roles) :]
        self._apps[(tenant, app)] = CedarDecisionPoint.from_manifests(
            manifests,
            tenant=tenant,
            ceilings={
                agent: Ceiling(
                    tools.allow,
                    grants.get(agent, nothing).read,
                    grants.get(agent, nothing).write,
                )
                for agent, tools in agents.items()
            },
        )

    def remove(self, tenant: str, app: str) -> None:
        self._apps.pop((tenant, app), None)
        self._sources.pop((tenant, app), None)

    def policy_set(
        self, tenant: str, app: str
    ) -> tuple[list[PolicyManifest], CompiledPolicies] | None:
        """An App's policies as written, and as Cedar runs them. The built-in roles aren't here."""
        engine = self._apps.get((tenant, app))
        if engine is None:
            return None
        return self._sources[(tenant, app)], engine.compiled

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        agent = request.actor.name if request.actor.kind == "agent" else None
        blocked = await self.store.blocking(request.tenant, app=request.app, agent=agent)
        if blocked is not None:
            return PolicyDecision(
                Effect.DENY,
                (f"suspension/{blocked.kind}/{blocked.name}",),
                reason=f"{blocked.kind} {blocked.name} is suspended: {blocked.reason}",
            )
        engine = self._control if request.action in CONTROL_ACTIONS else self._engine(request)
        if engine is None:
            return PolicyDecision(Effect.DENY, reason=f"no policies for App {request.app!r}")
        return await engine.decide(await self._with_delegate(request))

    def _engine(self, request: PolicyRequest) -> CedarDecisionPoint | None:
        return self._apps.get((request.tenant, request.app))

    async def _with_delegate(self, request: PolicyRequest) -> PolicyRequest:
        delegate = request.on_behalf_of
        if delegate is None:
            return request
        roles = await self.store.roles(request.tenant, delegate.principal)
        return replace(request, on_behalf_of=Actor(delegate.principal, roles))

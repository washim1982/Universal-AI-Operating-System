"""The policy decision point port (ADR-007; FR-SEC-03, 04, 05, 09).

Every enforcement point (publish, subscribe, dispatch, tool invocation, memory access, model
routing, approvals, admin) asks a :class:`PolicyDecisionPoint` before it acts. Requests have
Cedar's shape — principal, action, resource, context — so the Cedar engine in
:mod:`aios.security.cedar` plugs in directly, and another engine can sit behind the same port.
Anything a policy does not allow is denied.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import JsonValue


class PolicyAction(StrEnum):
    """Actions that policies govern in v0.1. WP 2.2 adds publish, subscribe, dispatch and more."""

    TOOL_INVOKE = "tool.invoke"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"


class SideEffect(StrEnum):
    """Tool side-effect classes (FR-TOOL-02); policies and approvals key off them."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXTERNAL_COMM = "external_comm"
    FINANCIAL = "financial"
    CODE_EXEC = "code_exec"


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    """Allowed once one of ``approvers`` approves; the run pauses until then (FR-HUM-01)."""


@dataclass(frozen=True, slots=True)
class Actor:
    """Who acts, as the policy engine sees them: an envelope principal plus its roles."""

    principal: str
    """``<kind>:<name>[@<tenant>]``, e.g. ``agent:research-agent@acme``."""
    roles: frozenset[str] = frozenset()

    @property
    def kind(self) -> str:
        return self.principal.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.principal.split(":", 1)[1].split("@", 1)[0]


@dataclass(frozen=True, slots=True)
class Resource:
    """What the action is applied to."""

    type: str
    id: str
    attributes: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])

    @classmethod
    def tool(cls, name: str, side_effect: SideEffect) -> Resource:
        """A tool such as ``stripe.refund.create`` with its side-effect class."""
        return cls("Tool", name, {"sideEffect": side_effect.value})

    @classmethod
    def memory_scope(cls, scope: str) -> Resource:
        return cls("MemoryScope", scope)


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    tenant: str
    app: str
    actor: Actor
    action: PolicyAction
    resource: Resource
    on_behalf_of: Actor | None = None
    """The user an agent acts for; the effective permission is the intersection (FR-SEC-05)."""
    tainted: bool = False
    """The run's context contains untrusted content (FR-SEC-09)."""
    approved: bool = False
    """An approver already approved this action (it is being retried after the approval)."""
    input: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    """The tool input, for conditions such as ``input.amountUSD > 100``."""


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: Effect
    policies: tuple[str, ...] = ()
    """Ids of the policy rules that determined the decision, for the audit log."""
    approvers: tuple[str, ...] = ()
    """Selectors such as ``role:support-lead`` when ``effect`` is REQUIRE_APPROVAL."""
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW


class PolicyDecisionPoint(ABC):
    @abstractmethod
    async def decide(self, request: PolicyRequest) -> PolicyDecision: ...


class AllowAll(PolicyDecisionPoint):
    """The Phase 1 default (ADR-007): enforcement points call the port from the start, and
    WP 2.2 swaps in the Cedar engine without touching them. Never use it in production."""

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        return PolicyDecision(Effect.ALLOW, reason="allow-all policy decision point")

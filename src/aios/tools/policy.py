"""The interim tool policy: agent manifests' ``tools.allow`` / ``tools.deny`` (WP 1.5).

It implements the same :class:`PolicyDecisionPoint` port as the Cedar engine
(:mod:`aios.security.cedar`), which replaces it in WP 2.2 without changes to the pipeline.
Default deny; ``deny`` wins; and, as FR-SEC-09 requires, while a run's context is tainted the
risky side-effect classes need approval.
"""

from __future__ import annotations

from collections.abc import Mapping

from aios.core.manifests import ToolAccess
from aios.core.policy import (
    Effect,
    PolicyAction,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyRequest,
    SideEffect,
)
from aios.core.topics import matches

TAINT_ESCALATED = frozenset(
    {SideEffect.DESTRUCTIVE, SideEffect.EXTERNAL_COMM, SideEffect.FINANCIAL, SideEffect.CODE_EXEC}
)


class StaticToolPolicy(PolicyDecisionPoint):
    def __init__(
        self,
        access: Mapping[tuple[str, str], ToolAccess],
        *,
        approvers: tuple[str, ...] = ("role:tenant-admin",),
    ) -> None:
        """``access`` maps ``(app, agent)`` to the agent's manifest ``spec.tools``."""
        self.access = dict(access)
        self.approvers = approvers

    def grant(self, app: str, agent: str, access: ToolAccess) -> None:
        self.access[(app, agent)] = access

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        if request.action is not PolicyAction.TOOL_INVOKE:
            return PolicyDecision(Effect.DENY, reason=f"{request.action} is not governed here")
        access = self.access.get((request.app, request.actor.name))
        name = request.resource.id
        if access is None:
            return PolicyDecision(Effect.DENY, reason=f"{request.actor.name} has no tool access")
        for pattern in access.deny:
            if matches(pattern, name):
                return PolicyDecision(
                    Effect.DENY, (f"deny:{pattern}",), reason="denied by manifest"
                )
        allowed = next((p for p in access.allow if matches(p, name)), None)
        if allowed is None:
            return PolicyDecision(Effect.DENY, reason="no allow rule matches (default deny)")
        side_effect = request.resource.attributes.get("sideEffect")
        if request.tainted and side_effect in {s.value for s in TAINT_ESCALATED}:
            return PolicyDecision(
                Effect.REQUIRE_APPROVAL,
                (f"allow:{allowed}", "taint"),
                self.approvers,
                f"{side_effect} tool while the context holds untrusted content",
            )
        return PolicyDecision(Effect.ALLOW, (f"allow:{allowed}",))

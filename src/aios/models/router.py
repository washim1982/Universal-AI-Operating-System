"""Rule-based model routing (FR-MOD-03; requirements §7.10).

The first rule whose ``match`` fits the request decides the candidate list; otherwise the
policy's ``default`` does. Data classification is enforced regardless of the rules:
``restricted`` data is only ever sent to ``private`` deployments (FR-SEC-11).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aios.core.envelope import DataClass
from aios.core.manifests import ModelPolicyManifest, ModelRule
from aios.core.ports import ModelRequest
from aios.models.registry import ModelEntry, ModelRegistry

logger = logging.getLogger("aios.models")


@dataclass(frozen=True, slots=True)
class Route:
    candidates: tuple[ModelEntry, ...]
    rule: str
    """``rules[<n>]`` or ``default``, for the audit trail."""
    skipped: tuple[str, ...] = ()
    """Candidates named by the policy but unusable here (unknown, placeholder or public)."""


class Router:
    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    def route(
        self,
        policy: ModelPolicyManifest,
        request: ModelRequest,
        *,
        agent: str | None = None,
        allow_placeholders: bool = False,
    ) -> Route:
        spec = policy.spec
        chosen: tuple[str, ...] = tuple(spec.default.candidates)
        rule_name = "default"
        require_private = request.data_class is DataClass.RESTRICTED
        for index, rule in enumerate(spec.rules):
            if _matches(rule, request, agent):
                chosen = tuple(rule.candidates)
                rule_name = f"rules[{index}]"
                require_private = require_private or rule.require.deployment == "private"
                break
        usable: list[ModelEntry] = []
        skipped: list[str] = []
        for candidate in chosen:
            entry = self.registry.get(candidate)
            if (
                entry is None
                or (entry.placeholder and not allow_placeholders)
                or (require_private and entry.deployment != "private")
            ):
                skipped.append(candidate)
            else:
                usable.append(entry)
        if skipped:
            logger.debug("policy %s %s: skipped %s", policy.metadata.name, rule_name, skipped)
        if spec.fallback == "none":
            usable = usable[:1]
        return Route(tuple(usable), rule_name, tuple(skipped))


def _matches(rule: ModelRule, request: ModelRequest, agent: str | None) -> bool:
    match = rule.match
    if match.task is not None and match.task != request.task:
        return False
    if match.data_class is not None and match.data_class != request.data_class:
        return False
    return match.agent is None or match.agent == agent

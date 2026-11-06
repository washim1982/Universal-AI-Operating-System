"""Rule-based model routing (FR-MOD-03; requirements §7.10).

The first rule whose ``match`` fits the request decides the candidate list; otherwise the
policy's ``default`` does. Data classification is enforced regardless of the rules:
``restricted`` data only ever goes to a ``private`` deployment, or to a ``zero-retention`` one,
which is a public model under a contract not to keep the data (FR-MOD-06). This covers fallbacks
too: what isn't approved never becomes a candidate.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from aios.core.envelope import DataClass
from aios.core.manifests import ModelPolicyManifest, ModelRule
from aios.core.ports import EmbeddingRequest, ModelRequest
from aios.models.registry import ModelEntry, ModelRegistry

logger = logging.getLogger("aios.models")

APPROVED_FOR_RESTRICTED = ("private", "zero-retention")


@dataclass(frozen=True, slots=True)
class Route:
    candidates: tuple[ModelEntry, ...]
    rule: str
    """``rules[<n>]`` or ``default``, for the audit trail."""
    skipped: tuple[str, ...] = ()
    """Candidates named by the policy but unusable here (unknown, placeholder or public)."""
    downgraded: bool = False
    """A soft budget limit chose this route."""


class Router:
    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    def route(
        self,
        policy: ModelPolicyManifest,
        request: ModelRequest | EmbeddingRequest,
        *,
        agent: str | None = None,
        allow_placeholders: bool = False,
        soft: bool = False,
        only: str | None = None,
    ) -> Route:
        """``soft`` means a soft limit calls for a downgrade: to a ``match.budget: soft`` rule if
        one fits, or else to the cheapest candidate first. It never stops the work by itself."""
        spec = policy.spec
        chosen: tuple[str, ...] = tuple(spec.default.candidates)
        rule_name = "default"
        restricted = request.data_class is DataClass.RESTRICTED
        required = None
        downgraded = False
        for index, rule in enumerate([] if only is not None else spec.rules):
            if _matches(rule, request, agent, soft):
                chosen = tuple(rule.candidates)
                rule_name = f"rules[{index}]"
                required = rule.require.deployment
                downgraded = rule.match.budget is not None
                break
        if only is not None:  # a fork's model: still checked like any other candidate
            chosen, rule_name, soft = (only,), "fork", False
        usable: list[ModelEntry] = []
        skipped: list[str] = []
        for candidate in chosen:
            entry = self.registry.get(candidate)
            if (
                entry is None
                or (entry.placeholder and not allow_placeholders)
                or (restricted and entry.deployment not in APPROVED_FOR_RESTRICTED)
                or (required is not None and entry.deployment != required)
            ):
                skipped.append(candidate)
            else:
                usable.append(entry)
        if downgraded and not usable:
            return self.route(
                policy, request, agent=agent, allow_placeholders=allow_placeholders, soft=False
            )
        if soft and not downgraded:
            cheapest = sorted(usable, key=_price)
            downgraded = cheapest != usable
            usable = cheapest
        if skipped:
            logger.debug("policy %s %s: skipped %s", policy.metadata.name, rule_name, skipped)
        if spec.fallback == "none":
            usable = usable[:1]
        return Route(tuple(usable), rule_name, tuple(skipped), downgraded)


def _price(entry: ModelEntry) -> float:
    """What a token costs. A model with no price goes last: nobody knows what it costs."""
    return math.inf if entry.prices is None else entry.prices.input + entry.prices.output


def _matches(
    rule: ModelRule, request: ModelRequest | EmbeddingRequest, agent: str | None, soft: bool
) -> bool:
    match = rule.match
    if match.budget is not None and not soft:
        return False
    if match.task is not None and match.task != request.task:
        return False
    if match.data_class is not None and match.data_class != request.data_class:
        return False
    return match.agent is None or match.agent == agent

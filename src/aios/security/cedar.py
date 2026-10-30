"""The Cedar policy engine (ADR-007): compile AIOS Policy manifests to Cedar and decide requests.

Requires the ``policy`` extra (``cedarpy``, the Python binding of the Rust Cedar library).

How AIOS concepts map to Cedar:

* **Entities.** ``Tenant``; ``App`` and ``Role`` in a tenant; ``Agent`` in its App and roles;
  ``User`` in its roles; ``Service``; ``System``. Policy subjects become ``principal ==`` (agent,
  user) or ``principal in`` (app, role) constraints.
* **Tools.** A ``Tool`` entity carries its name split into ``provider`` / ``object`` / ``verb``
  plus its ``sideEffect`` class. Tool patterns compile to equality checks on those tokens, which
  keeps the topic-grammar semantics exact: Cedar's ``like "a.*.c"`` would let ``*`` span tokens.
* **Approvals.** A ``requireApproval`` rule compiles to ``forbid … unless { context.approved }``.
  A request that only such rules deny, and that is allowed once approved, needs approval.
* **Fail closed.** Evaluation errors, requests that don't match the schema and tool inputs that
  can't be encoded are denied. Only the input fields that policies reference are passed to Cedar.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import cedarpy

from aios.core.errors import InvalidPolicyError
from aios.core.manifests import ApprovalRule, Comparison, PolicyManifest, PolicyRule
from aios.core.policy import (
    Actor,
    Effect,
    PolicyAction,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyRequest,
)
from aios.security.cedar_schema import InputType, schema_text

__all__ = [
    "CedarDecisionPoint",
    "CompiledPolicies",
    "CompiledRule",
    "compile_policies",
    "schema_text",
]

_TOOL_FIELDS = ("provider", "object", "verb")
_PRINCIPAL_TYPES = {
    "agent": "Agent",
    "user": "User",
    "app": "App",
    "service": "Service",
    "system": "System",
}
_SUBJECTS = {"agent": "== Agent", "user": "== User", "app": "in App", "role": "in Role"}
_DECIMAL_OPERATORS = {
    "gt": "greaterThan",
    "gte": "greaterThanOrEqual",
    "lt": "lessThan",
    "lte": "lessThanOrEqual",
}
_DECIMAL_PLACES = Decimal("0.0001")
_DECIMAL_LIMIT = Decimal("922337203685477.5807")  # Cedar's decimal range

RuleKind = Literal["allow", "deny", "approval"]


@dataclass(frozen=True, slots=True)
class CompiledRule:
    id: str
    """``<policy name>/<allow|deny|approval>/<index>``; recorded with every decision."""
    kind: RuleKind
    approvers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompiledPolicies:
    text: str
    """The Cedar policy set."""
    schema: str
    rules: tuple[CompiledRule, ...]
    """In policy-set order, so Cedar's ``policy<N>`` ids index this tuple."""
    input_fields: Mapping[str, InputType] = field(default_factory=dict[str, InputType])


def compile_policies(manifests: Iterable[PolicyManifest]) -> CompiledPolicies:
    """Compile Policy manifests to one Cedar policy set and validate it against the schema."""
    compiler = _Compiler()
    for manifest in manifests:
        compiler.add(manifest)
    return compiler.finish()


class _Compiler:
    def __init__(self) -> None:
        self.policies: list[str] = []
        self.rules: list[CompiledRule] = []
        self.input_fields: dict[str, InputType] = {}

    def add(self, manifest: PolicyManifest) -> None:
        spec = manifest.spec
        groups: Sequence[tuple[RuleKind, Sequence[PolicyRule]]] = (
            ("allow", spec.allow),
            ("deny", spec.deny),
            ("approval", spec.require_approval),
        )
        subject_type = _SUBJECTS[spec.subject.kind]
        principal = f"principal {subject_type}::{_string(spec.subject.name)}"
        for kind, rules in groups:
            for index, rule in enumerate(rules):
                rule_id = f"{manifest.metadata.name}/{kind}/{index}"
                approvers = tuple(rule.approvers) if isinstance(rule, ApprovalRule) else ()
                self.policies.append(self._policy(rule_id, kind, principal, rule))
                self.rules.append(CompiledRule(rule_id, kind, approvers))

    def _policy(self, rule_id: str, kind: RuleKind, principal: str, rule: PolicyRule) -> str:
        if rule.action is PolicyAction.TOOL_INVOKE:
            resource, conditions = _tool_constraints(rule)
        else:
            scopes = ", ".join(f"MemoryScope::{_string(scope)}" for scope in rule.scopes or ())
            resource, conditions = "resource", [f"[{scopes}].contains(resource)"]
        if rule.when.context_tainted is not None:
            conditions.append(
                "context.tainted" if rule.when.context_tainted else "!context.tainted"
            )
        for name, comparison in sorted(rule.when.input.items()):
            conditions.append(self._comparison(rule_id, name, comparison))

        effect = "permit" if kind == "allow" else "forbid"
        lines = [
            f"@id({_string(rule_id)})",
            f"{effect} (",
            f"  {principal},",
            f"  action == Action::{_string(rule.action.value)},",
            f"  {resource}",
            ")",
        ]
        if conditions:
            lines.append("when { " + " && ".join(conditions) + " }")
        if kind == "approval":
            lines.append("unless { context.approved }")
        return "\n".join(lines) + ";"

    def _comparison(self, rule_id: str, name: str, comparison: Comparison) -> str:
        value = comparison.value
        kind: InputType = (
            "Bool" if isinstance(value, bool) else "String" if isinstance(value, str) else "decimal"
        )
        if self.input_fields.setdefault(name, kind) != kind:
            raise InvalidPolicyError(
                f"{rule_id}: input.{name} is compared as {kind} here and as "
                f"{self.input_fields[name]} elsewhere"
            )
        field_ref = f"context.input.{name}"
        guard = f"context.input has {name}"
        if kind == "decimal":
            number = _decimal(value)
            if number is None:
                raise InvalidPolicyError(
                    f"{rule_id}: input.{name} threshold {value!r} needs at most 4 decimal places "
                    "and must fit Cedar's decimal range"
                )
            literal = f'decimal("{number:.4f}")'
            if comparison.operator == "eq":
                return f"{guard} && {field_ref} == {literal}"
            return f"{guard} && {field_ref}.{_DECIMAL_OPERATORS[comparison.operator]}({literal})"
        # Strings and booleans only support eq: Comparison types gt/gte/lt/lte as numbers.
        literal = ("true" if value else "false") if isinstance(value, bool) else _string(str(value))
        return f"{guard} && {field_ref} == {literal}"

    def finish(self) -> CompiledPolicies:
        text = "\n\n".join(self.policies) + "\n"
        schema = schema_text(self.input_fields)
        result = cedarpy.validate_policies(text, schema)  # pyright: ignore[reportUnknownMemberType]
        if not result.validation_passed:
            errors = "; ".join(error.error for error in result.errors)
            raise InvalidPolicyError(f"compiled policies fail Cedar validation: {errors}")
        return CompiledPolicies(text, schema, tuple(self.rules), dict(self.input_fields))


def _tool_constraints(rule: PolicyRule) -> tuple[str, list[str]]:
    """The resource scope and conditions for a tool.invoke rule."""
    resource = "resource"
    conditions: list[str] = []
    if rule.resource is not None:
        tokens = rule.resource.split(".")
        if "*" not in tokens and ">" not in tokens:
            resource = f"resource == Tool::{_string(rule.resource)}"
        else:
            for name, token in zip(_TOOL_FIELDS, tokens, strict=False):
                if token == ">":
                    break
                if token != "*":
                    conditions.append(f"resource.{name} == {_string(token)}")
    if rule.when.side_effect is not None:
        classes = ", ".join(_string(effect.value) for effect in rule.when.side_effect)
        conditions.append(f"[{classes}].contains(resource.sideEffect)")
    return resource, conditions


def _string(value: str) -> str:
    """A Cedar string literal. Names reaching here are already validated identifiers."""
    if any(ord(char) < 0x20 for char in value):
        raise InvalidPolicyError(f"control characters are not allowed in policy strings: {value!r}")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decimal(value: object) -> Decimal | None:
    """``value`` as a Cedar decimal, or None if it isn't exactly representable."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:  # pragma: no cover - str() of a finite number always parses
        return None
    if not number.is_finite() or abs(number) > _DECIMAL_LIMIT:
        return None
    if number != number.quantize(_DECIMAL_PLACES):
        return None
    return number


# --------------------------------------------------------------------------- decisions


class _Rejected(Exception):
    """The request can't be evaluated safely; the decision is deny."""


class CedarDecisionPoint(PolicyDecisionPoint):
    """Decides requests for one tenant against a compiled policy set."""

    def __init__(self, compiled: CompiledPolicies, *, tenant: str) -> None:
        self.compiled = compiled
        self.tenant = tenant
        self._policies = cedarpy.PolicySet.from_str(compiled.text)
        self._schema = cedarpy.Schema.from_str(compiled.schema)

    @classmethod
    def from_manifests(
        cls, manifests: Iterable[PolicyManifest], *, tenant: str
    ) -> CedarDecisionPoint:
        return cls(compile_policies(manifests), tenant=tenant)

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        return self.decide_now(request)

    def decide_now(self, request: PolicyRequest) -> PolicyDecision:
        """The synchronous decision; Cedar runs in-process in microseconds."""
        if request.tenant != self.tenant:
            return PolicyDecision(Effect.DENY, reason=f"policies of tenant {self.tenant!r} only")
        try:
            decisions = [self._evaluate(request, request.actor)]
            if request.on_behalf_of is not None:  # delegation: agent ∩ user (FR-SEC-05)
                decisions.append(self._evaluate(request, request.on_behalf_of))
        except _Rejected as exc:
            return PolicyDecision(Effect.DENY, reason=str(exc))
        return _intersect(decisions)

    def _evaluate(self, request: PolicyRequest, actor: Actor) -> PolicyDecision:
        entities = self._entities(request, actor)
        context: dict[str, Any] = {"tainted": request.tainted, "approved": request.approved}
        if request.action is PolicyAction.TOOL_INVOKE:
            context["input"] = self._input(request.input)
        cedar_request = {
            "principal": self._principal(actor),
            "action": {"type": "Action", "id": request.action.value},
            "resource": {"type": request.resource.type, "id": request.resource.id},
            "context": context,
        }
        result = self._authorize(cedar_request, entities)
        if result.decision == cedarpy.Decision.Allow:
            return PolicyDecision(Effect.ALLOW, self._rule_ids(result))
        rules = self._rules(result)
        if rules and not request.approved and all(rule.kind == "approval" for rule in rules):
            retried = self._authorize(
                {**cedar_request, "context": {**context, "approved": True}}, entities
            )
            if retried.decision == cedarpy.Decision.Allow:
                approvers = tuple(dict.fromkeys(a for rule in rules for a in rule.approvers))
                return PolicyDecision(
                    Effect.REQUIRE_APPROVAL,
                    tuple(rule.id for rule in rules),
                    approvers,
                    "allowed once approved",
                )
        return PolicyDecision(
            Effect.DENY,
            tuple(rule.id for rule in rules),
            reason="denied by policy" if rules else "no policy allows this (default deny)",
        )

    def _authorize(self, cedar_request: dict[str, Any], entities: list[dict[str, Any]]) -> Any:
        result = cedarpy.is_authorized(  # pyright: ignore[reportUnknownMemberType]
            cedar_request, self._policies, entities, schema=self._schema
        )
        if result.diagnostics.errors or result.decision == cedarpy.Decision.NoDecision:
            raise _Rejected(
                "policy evaluation failed: " + "; ".join(map(str, result.diagnostics.errors))
            )
        return result

    def _rules(self, result: Any) -> list[CompiledRule]:
        return [
            self.compiled.rules[int(reason.removeprefix("policy"))]
            for reason in result.diagnostics.reasons
        ]

    def _rule_ids(self, result: Any) -> tuple[str, ...]:
        return tuple(rule.id for rule in self._rules(result))

    @staticmethod
    def _principal(actor: Actor) -> dict[str, str]:
        kind = _PRINCIPAL_TYPES.get(actor.kind)
        if kind is None:
            raise _Rejected(f"unknown principal kind in {actor.principal!r}")
        return {"type": kind, "id": actor.name}

    def _entities(self, request: PolicyRequest, actor: Actor) -> list[dict[str, Any]]:
        tenant = {"type": "Tenant", "id": request.tenant}
        app = {"type": "App", "id": request.app}
        roles = [{"type": "Role", "id": role} for role in sorted(actor.roles)]
        principal = self._principal(actor)
        parents: dict[str, list[dict[str, str]]] = {
            "Agent": [app, tenant, *roles],
            "User": [tenant, *roles],
            "App": [tenant],
            "Service": [tenant],
            "System": [],
        }
        entities: list[dict[str, Any]] = [
            {"uid": tenant, "attrs": {}, "parents": []},
            *({"uid": role, "attrs": {}, "parents": [tenant]} for role in roles),
            {"uid": principal, "attrs": {}, "parents": parents[principal["type"]]},
            self._resource(request),
        ]
        if principal != app:
            entities.append({"uid": app, "attrs": {}, "parents": [tenant]})
        return entities

    @staticmethod
    def _resource(request: PolicyRequest) -> dict[str, Any]:
        resource = request.resource
        uid = {"type": resource.type, "id": resource.id}
        if resource.type != "Tool":
            return {"uid": uid, "attrs": {}, "parents": []}
        tokens = resource.id.split(".")
        side_effect = resource.attributes.get("sideEffect")
        if len(tokens) != len(_TOOL_FIELDS) or not isinstance(side_effect, str):
            raise _Rejected(
                f"tool {resource.id!r} needs a <provider>.<object>.<verb> name and a sideEffect"
            )
        attrs: dict[str, Any] = dict(zip(_TOOL_FIELDS, tokens, strict=True))
        attrs.update(name=resource.id, sideEffect=side_effect)
        return {"uid": uid, "attrs": attrs, "parents": []}

    def _input(self, values: Mapping[str, Any]) -> dict[str, Any]:
        encoded: dict[str, Any] = {}
        for name, kind in self.compiled.input_fields.items():
            if name not in values:
                continue
            value = values[name]
            if kind == "decimal":
                number = _decimal(value)
                if number is None:
                    raise _Rejected(f"input.{name} must be a number with at most 4 decimal places")
                encoded[name] = {"__extn": {"fn": "decimal", "arg": f"{number:.4f}"}}
            elif not isinstance(value, bool if kind == "Bool" else str):
                raise _Rejected(f"input.{name} must be a {kind.lower()}")
            else:
                encoded[name] = value
        return encoded


def _intersect(decisions: Sequence[PolicyDecision]) -> PolicyDecision:
    """Combine the agent's and the user's decisions: both must allow (FR-SEC-05)."""
    for decision in decisions:
        if decision.effect is Effect.DENY:
            return decision
    approvals = [d for d in decisions if d.effect is Effect.REQUIRE_APPROVAL]
    if not approvals:
        return PolicyDecision(Effect.ALLOW, tuple(p for d in decisions for p in d.policies))
    return PolicyDecision(
        Effect.REQUIRE_APPROVAL,
        tuple(dict.fromkeys(p for d in approvals for p in d.policies)),
        tuple(dict.fromkeys(a for d in approvals for a in d.approvers)),
        "allowed once approved",
    )

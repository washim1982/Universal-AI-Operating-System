from typing import Any

import pytest
from pydantic import ValidationError

from aios.core.manifests import PolicyManifest, interpolate_config, load_manifests, parse_manifest
from aios.core.policy import (
    Actor,
    AllowAll,
    Effect,
    PolicyAction,
    PolicyRequest,
    Resource,
    SideEffect,
)

POLICY: dict[str, Any] = {
    "apiVersion": "aios/v1alpha1",
    "kind": "Policy",
    "metadata": {"name": "p", "version": "1.0.0"},
    "spec": {"subject": {"agent": "a"}, "allow": [{"action": "tool.invoke", "resource": "x.>"}]},
}


def _with_rule(rule: dict[str, Any], *, group: str = "allow") -> dict[str, Any]:
    return {**POLICY, "spec": {"subject": {"agent": "a"}, group: [rule]}}


def test_policy_manifest() -> None:
    manifest = parse_manifest(
        _with_rule(
            {
                "action": "tool.invoke",
                "when": {"sideEffect": ["financial"], "input": {"amountUSD": {"gt": 100}}},
                "approvers": ["role:lead"],
            },
            group="requireApproval",
        )
    )
    assert isinstance(manifest, PolicyManifest)
    [rule] = manifest.spec.require_approval
    assert rule.when.side_effect == [SideEffect.FINANCIAL]
    assert (rule.when.input["amountUSD"].operator, rule.when.input["amountUSD"].value) == (
        "gt",
        100,
    )
    assert manifest.spec.subject.kind == "agent"
    assert manifest.spec.subject.name == "a"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({**POLICY, "spec": {"subject": {"agent": "a"}}}, "at least one"),
        ({**POLICY, "spec": {**POLICY["spec"], "subject": {}}}, "exactly one of agent"),
        (
            {**POLICY, "spec": {**POLICY["spec"], "subject": {"agent": "a", "app": "b"}}},
            "exactly one of agent",
        ),
        (_with_rule({"action": "tool.invoke", "scopes": ["app"]}), "not 'scopes'"),
        (_with_rule({"action": "memory.read"}), "need 'scopes'"),
        (
            _with_rule({"action": "memory.read", "scopes": ["app"], "resource": "x.>"}),
            "not 'resource'",
        ),
        (
            _with_rule(
                {"action": "memory.read", "scopes": ["app"], "when": {"sideEffect": ["read"]}}
            ),
            "tool.invoke only",
        ),
        (_with_rule({"action": "tool.invoke", "resource": "x.*"}), "did you mean 'x.>'"),
        (_with_rule({"action": "admin.everything"}), "Input should be"),
        (
            _with_rule({"action": "tool.invoke", "when": {"input": {"n": {"gt": 1, "lt": 5}}}}),
            "exactly one of gt",
        ),
        (_with_rule({"action": "tool.invoke", "when": {"input": {"in": {"eq": 1}}}}), "reserved"),
        (
            _with_rule({"action": "tool.invoke", "when": {"input": {"a.b": {"eq": 1}}}}),
            "should match",
        ),
        (_with_rule({"action": "tool.invoke"}, group="requireApproval"), "approvers"),
    ],
)
def test_policy_validation(document: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_manifest(document)


def test_interpolate_config_keeps_types_of_whole_placeholders() -> None:
    config = {"threshold": 50, "role": "support-lead"}
    document = {"gt": "${config.threshold}", "approvers": ["role:${config.role}"], "n": 3}
    assert interpolate_config(document, config) == {
        "gt": 50,
        "approvers": ["role:support-lead"],
        "n": 3,
    }
    with pytest.raises(ValueError, match="no config value"):
        interpolate_config("${config.missing}", config)


def test_load_manifests_interpolates_config() -> None:
    text = """
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: p, version: 1.0.0}
spec:
  subject: {app: desk}
  requireApproval:
    - action: tool.invoke
      when: {input: {amountUSD: {gt: "${config.limit}"}}}
      approvers: ["role:${config.role}"]
"""
    [manifest] = load_manifests(text, config={"limit": 25.5, "role": "lead"})
    assert isinstance(manifest, PolicyManifest)
    [rule] = manifest.spec.require_approval
    assert rule.when.input["amountUSD"].value == 25.5
    assert rule.approvers == ["role:lead"]
    with pytest.raises(ValidationError):
        load_manifests(text)  # the placeholder is not a number


async def test_allow_all_is_permissive() -> None:
    request = PolicyRequest(
        tenant="acme",
        app="desk",
        actor=Actor("agent:refund-agent@acme"),
        action=PolicyAction.TOOL_INVOKE,
        resource=Resource.tool("stripe.refund.create", SideEffect.FINANCIAL),
    )
    decision = await AllowAll().decide(request)
    assert decision.allowed
    assert decision.effect is Effect.ALLOW


def test_actor_parts() -> None:
    actor = Actor("agent:research-agent@acme", frozenset({"researcher"}))
    assert (actor.kind, actor.name) == ("agent", "research-agent")
    assert Actor("user:alice").name == "alice"

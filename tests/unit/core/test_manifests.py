import copy
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from aios.core.manifests import (
    AgentManifest,
    AppManifest,
    WorkflowManifest,
    format_duration,
    load_manifests,
    parse_manifest,
)

AGENT: dict[str, Any] = {
    "apiVersion": "aios/v1alpha1",
    "kind": "Agent",
    "metadata": {"name": "research-agent", "version": "1.4.0"},
    "spec": {
        "runtime": {"type": "python", "entrypoint": "agents.research:research"},
        "capabilities": [{"name": "research.web"}],
        "limits": {"maxSteps": 20, "maxCostUSD": 1.5, "timeout": "5m"},
    },
}

WORKFLOW_YAML = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: research-report, version: 1.0.0}
spec:
  steps:
    - {id: plan, type: task, capability: planning}
    - {id: research, type: dynamic, capability: research.*, fromPlan: plan.output.tasks,
       maxParallel: 5}
    - {id: analyze, type: task, capability: analysis}
    - {id: draft, type: task, capability: writing}
    - {id: review, type: task, capability: review, onResult: {reject: draft}, maxLoops: 2}
    - {id: publish-approval, type: approval, approvers: ["role:editor"], timeout: 24h,
       onTimeout: deny}
    - {id: publish, type: tool, tool: cms.page.publish}
  budget: {maxCostUSD: 3.00, maxDuration: 30m}
"""


def _with(base: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Copy ``base`` with the dotted ``path`` set to ``value``."""
    result = copy.deepcopy(base)
    node = result
    *parents, leaf = path.split(".")
    for key in parents:
        node = node[key]
    node[leaf] = value
    return result


def test_agent_manifest() -> None:
    manifest = parse_manifest(AGENT)
    assert isinstance(manifest, AgentManifest)
    assert manifest.spec.limits.max_cost_usd == 1.5
    assert manifest.spec.limits.timeout == timedelta(minutes=5)
    assert manifest.spec.isolation == "process"
    dumped = manifest.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert dumped["spec"]["limits"] == {"maxSteps": 20, "maxCostUSD": 1.5, "timeout": "5m"}


def test_workflow_manifest_mirrors_the_dsl_example() -> None:
    [manifest] = load_manifests(WORKFLOW_YAML)
    assert isinstance(manifest, WorkflowManifest)
    assert [s.type for s in manifest.spec.steps] == [
        "task", "dynamic", "task", "task", "task", "approval", "tool"
    ]  # fmt: skip
    assert manifest.spec.budget.max_duration == timedelta(minutes=30)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("spec.runtime", {"type": "python"}, "requires 'entrypoint'"),
        ("spec.runtime", {"type": "python", "entrypoint": "no-colon"}, "module.path:function"),
        ("spec.runtime", {"type": "container"}, "requires 'image'"),
        ("spec.capabilities", [], "at least 1"),
        ("spec.capabilities", [{"name": "research.*"}], "invalid token"),
        ("spec.tools", {"allow": ["search.*"]}, "did you mean 'search.>'"),
        ("spec.tools", {"allow": ["filesystem.read"]}, "can never match"),
        ("spec.limits", {"timeout": 30}, "invalid duration"),
        ("spec.scaling", {"min": 5, "max": 2}, "must not exceed"),
        ("spec.limits", {"timeout": "5 minutes"}, "invalid duration"),
        ("spec.isolation", "vm", "Input should be"),
        ("metadata.name", "Research_Agent", "String should match"),
        ("metadata.version", "1.4", "String should match"),
        ("spec.unknown", 1, "Extra inputs"),
    ],
)
def test_agent_validation(path: str, value: Any, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_manifest(_with(AGENT, path, value))


def test_tool_patterns_accept_three_token_matches() -> None:
    manifest = parse_manifest(
        _with(AGENT, "spec.tools", {"allow": ["search.>", "http.page.fetch", "filesystem.*.read"]})
    )
    assert isinstance(manifest, AgentManifest)


def test_too_long_tool_patterns_get_no_misleading_hint() -> None:
    with pytest.raises(ValidationError, match="can never match") as raised:
        parse_manifest(_with(AGENT, "spec.tools", {"allow": ["zendesk.ticket.get.*"]}))
    assert "did you mean" not in str(raised.value)


def test_app_entrypoints_must_use_declared_workflows() -> None:
    app: dict[str, Any] = {
        "apiVersion": "aios/v1alpha1",
        "kind": "App",
        "metadata": {"name": "demo", "version": "0.1.0"},
        "spec": {"entrypoints": [{"type": "chat", "workflow": "missing"}], "workflows": []},
    }
    with pytest.raises(ValidationError, match=r"not listed in spec\.workflows"):
        parse_manifest(app)
    app["spec"]["workflows"] = ["missing"]
    assert isinstance(parse_manifest(app), AppManifest)


def test_app_tool_refs_need_exactly_one_of_provider_or_name() -> None:
    app = {
        "apiVersion": "aios/v1alpha1",
        "kind": "App",
        "metadata": {"name": "demo", "version": "0.1.0"},
        "spec": {
            "entrypoints": [{"type": "chat", "workflow": "w"}],
            "workflows": ["w"],
            "tools": [{"provider": "zendesk", "name": "zendesk.ticket.get"}],
        },
    }
    with pytest.raises(ValidationError, match="exactly one"):
        parse_manifest(app)


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([{"id": "a", "type": "task", "capability": "x"}] * 2, "duplicate step id"),
        ([{"id": "a", "type": "task", "capability": "x", "needs": ["b"]}], "unknown step"),
        (
            [
                {"id": "a", "type": "task", "capability": "x", "needs": ["b"]},
                {"id": "b", "type": "task", "capability": "x"},
            ],
            "needs later step",
        ),
        (
            [
                {
                    "id": "a",
                    "type": "task",
                    "capability": "x",
                    "onResult": {"no": "a"},
                    "maxLoops": 1,
                }
            ],
            "earlier step",
        ),
        (
            [
                {"id": "a", "type": "task", "capability": "x"},
                {"id": "b", "type": "task", "capability": "x", "onResult": {"no": "a"}},
            ],
            "max_loops is 0",
        ),
        (
            [{"id": "a", "type": "dynamic", "capability": "x", "fromPlan": "plan.output.tasks"}],
            "must read an earlier step",
        ),
        ([{"id": "a", "type": "approval", "approvers": list[str]()}], "at least 1"),
        ([{"id": "a", "type": "tool", "tool": "cms.publish"}], "provider"),
    ],
)
def test_workflow_graph_validation(steps: list[dict[str, Any]], message: str) -> None:
    document = {
        "apiVersion": "aios/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "w", "version": "1.0.0"},
        "spec": {"steps": steps},
    }
    with pytest.raises(ValidationError, match=message):
        parse_manifest(document)


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_manifest({"apiVersion": "aios/v1alpha1", "kind": "Policy", "metadata": {}})


def test_yaml_on_key_is_not_a_boolean() -> None:
    text = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 0.3.0}
spec:
  entrypoints:
    - type: event
      on: zendesk.ticket.created
      workflow: handle-ticket
  workflows: [handle-ticket]
"""
    [manifest] = load_manifests(text)
    assert isinstance(manifest, AppManifest)
    assert manifest.spec.entrypoints[0].type == "event"


def test_multi_document_yaml() -> None:
    assert len(load_manifests(WORKFLOW_YAML + "\n---\n" + WORKFLOW_YAML)) == 2


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (timedelta(milliseconds=500), "500ms"),
        (timedelta(seconds=90), "90s"),
        (timedelta(minutes=5), "5m"),
        (timedelta(hours=24), "1d"),
        (timedelta(hours=36), "36h"),
    ],
)
def test_duration_formatting(value: timedelta, text: str) -> None:
    assert format_duration(value) == text

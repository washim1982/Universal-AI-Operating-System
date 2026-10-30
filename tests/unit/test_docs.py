"""Keep the documents and the code from drifting apart."""

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from aios.cli.main import generated_specs
from aios.core.manifests import (
    AgentManifest,
    AppManifest,
    DynamicStep,
    PolicyManifest,
    TaskStep,
    WorkflowManifest,
    load_manifests,
)
from aios.core.schemas import stale_specs
from aios.security.cedar import compile_policies

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "project-requirement-v2.md"
TRACEABILITY = ROOT / "docs" / "traceability.md"
KINDS = {"Agent", "App", "Workflow", "Policy", "ModelPolicy"}  # kinds aios.core.manifests validates


def _blocks(markdown: str, language: str = "yaml") -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", markdown, flags=re.DOTALL)


def _requirement_examples() -> list[str]:
    examples: list[str] = []
    for block in _blocks(REQUIREMENTS.read_text(encoding="utf-8")):
        document: Any = yaml.safe_load(block)
        if isinstance(document, dict) and cast(dict[str, Any], document).get("kind") in KINDS:
            examples.append(block)
    return examples


def _app_config(app: Path) -> dict[str, Any]:
    """Default values from the App's config schema, as an install without overrides gets."""
    path = app / "config.schema.json"
    if not path.exists():
        return {}
    properties = json.loads(path.read_text(encoding="utf-8"))["properties"]
    return {name: spec["default"] for name, spec in properties.items() if "default" in spec}


def test_generated_specs_are_current() -> None:
    stale = stale_specs(ROOT / "specs", generated_specs())
    assert not stale, f"run `aios specs export`: {[p.name for p in stale]}"


def test_requirement_manifest_examples_are_valid() -> None:
    examples = _requirement_examples()
    assert len(examples) >= 4  # §7.3 Agent, §7.4 App, §7.9 Policy, §7.10 ModelPolicy
    for block in examples:
        assert load_manifests(block)


def test_requirement_cedar_excerpt_matches_the_compiler() -> None:
    [policy] = [
        m for block in _requirement_examples() for m in load_manifests(block)
        if isinstance(m, PolicyManifest)
    ]  # fmt: skip
    compiled = compile_policies([policy]).text
    [excerpt] = _blocks(REQUIREMENTS.read_text(encoding="utf-8"), "cedar")
    conditions = [line for line in excerpt.splitlines() if line.startswith(("when", "unless"))]
    assert len(conditions) >= 3
    for line in conditions:
        assert line.removesuffix(";") in compiled, line


@pytest.mark.parametrize("app", ["ai-researcher", "ai-support-desk"])
def test_reference_app_manifests_are_valid_and_consistent(app: str) -> None:
    manifests = [m for f in sorted((ROOT / "apps" / app / "manifests").glob("*.yaml"))
                 for m in load_manifests(f)]  # fmt: skip
    [app_manifest] = [m for m in manifests if isinstance(m, AppManifest)]
    agents = {m.metadata.name for m in manifests if isinstance(m, AgentManifest)}
    workflows = [m for m in manifests if isinstance(m, WorkflowManifest)]
    assert app_manifest.metadata.name == app
    assert set(app_manifest.spec.agents) == agents
    assert set(app_manifest.spec.workflows) == {w.metadata.name for w in workflows}

    provided = {
        c.name for m in manifests if isinstance(m, AgentManifest) for c in m.spec.capabilities
    }
    for workflow in workflows:
        for step in workflow.spec.steps:
            if isinstance(step, TaskStep | DynamicStep):
                assert step.capability in provided, f"{workflow.metadata.name}/{step.id}"

    folder = ROOT / "apps" / app
    policies = [
        m for f in sorted((folder / "policies").glob("*.yaml"))
        for m in load_manifests(f, config=_app_config(folder))
    ]  # fmt: skip
    assert all(isinstance(p, PolicyManifest) for p in policies)
    assert set(app_manifest.spec.policies) == {p.metadata.name for p in policies}
    if policies:
        compile_policies(p for p in policies if isinstance(p, PolicyManifest))


def test_traceability_lists_every_requirement() -> None:
    defined = set(re.findall(r"^\| (FR-[A-Z]+-\d{2}) \|", REQUIREMENTS.read_text("utf-8"), re.M))
    traced = set(re.findall(r"^\| (FR-[A-Z]+-\d{2}) \|", TRACEABILITY.read_text("utf-8"), re.M))
    assert defined, "no requirement ids found"
    assert defined - traced == set(), "missing from docs/traceability.md"
    assert traced - defined == set(), "unknown ids in docs/traceability.md"

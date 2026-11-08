"""A workflow in Python, YAML or JSON is one graph IR (FR-WF-01; WP 3.6)."""

import json
from pathlib import Path

from aios.controlplane import load_app
from aios.controlplane.bundle import bundle_of, rendered
from aios.core.manifests import WorkflowManifest, load_manifests
from aios.sdk import Approval, Step, Workflow

YAML = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: refund, version: 1.0.0}
spec:
  output: "${pay.output}"
  steps:
    - {id: read, type: task, capability: refunds.read, input: {message: "${input.message}"}}
    - {id: check, type: approval, approvers: ["role:lead"]}
    - {id: pay, type: task, capability: refunds.pay, input: {order: "${read.output.order}"}}
"""


def _python() -> WorkflowManifest:
    return (
        Workflow("refund", version="1.0.0")
        .then(Step("read", capability="refunds.read", input={"message": "${input.message}"}))
        .then(Approval("check", approvers=["role:lead"]))
        .then(Step("pay", capability="refunds.pay", input={"order": "${read.output.order}"}))
        .output("${pay.output}")
        .compile()
    )


def test_python_yaml_and_json_are_the_same_workflow() -> None:
    [from_yaml] = load_manifests(YAML)
    as_json = json.dumps(from_yaml.model_dump(mode="json", by_alias=True, exclude_none=True))
    [from_json] = load_manifests(as_json)
    assert from_yaml == from_json
    assert _python() == from_yaml


def test_a_json_file_may_hold_several_manifests() -> None:
    [workflow] = load_manifests(YAML)
    other = workflow.model_copy(
        update={"metadata": workflow.metadata.model_copy(update={"name": "other"})}
    )
    text = json.dumps(
        [m.model_dump(mode="json", by_alias=True, exclude_none=True) for m in (workflow, other)]
    )
    assert [m.metadata.name for m in load_manifests(text)] == ["refund", "other"]


APP = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: refunds, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: refund}]
  workflows: [refund]
  humans:
    - {capability: refunds.read, assignees: ["role:lead"]}
    - {capability: refunds.pay, assignees: ["role:lead"]}
"""


def test_an_app_folder_reads_json_as_well_as_yaml(tmp_path: Path) -> None:
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "app.yaml").write_text(APP, encoding="utf-8")
    [workflow] = load_manifests(YAML)
    (tmp_path / "manifests" / "workflow.json").write_text(
        json.dumps(workflow.model_dump(mode="json", by_alias=True, exclude_none=True)),
        encoding="utf-8",
    )
    names = sorted(type(m).__name__ for m in load_app(tmp_path))
    assert names == ["AppManifest", "WorkflowManifest"]
    bundle = bundle_of(tmp_path)  # a bundle is one stream of YAML documents
    kinds = sorted(type(m).__name__ for m in rendered(bundle.documents, None, None))
    assert kinds == names

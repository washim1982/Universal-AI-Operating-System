"""Experiments over HTTP (FR-OBS-07): who may run, read and grade them."""

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import httpx2
import pytest

from aios.api import create_app
from aios.core.identity import Identity
from aios.core.manifests import load_manifests
from aios.kernel import Context
from aios.security.identity import ApiKeys
from aios.testing.runtime import LocalRuntime

UNKNOWN = "01J0000000000000000000000A"
DESK = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: answer}]
  agents: [answerer]
  workflows: [answer]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: answerer, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "eval_api_desk:answer"}
  capabilities: [{name: desk.answer}]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: answer, version: 1.0.0}
spec:
  steps:
    - {id: answer, type: task, capability: desk.answer, input: {message: "${input.message}"}}
"""
SUITE: dict[str, Any] = {
    "apiVersion": "aios/v1alpha1",
    "kind": "Evaluation",
    "metadata": {"name": "manners", "version": "1.0.0"},
    "spec": {
        "cases": [{"id": "hi", "input": {"message": "hi"}}],
        "graders": [
            {"name": "polite", "type": "contains", "path": "answer", "value": "hello"},
            {"name": "tone", "type": "human", "reviewers": ["role:qa"]},
        ],
    },
}


async def _answer(ctx: Context, task: dict[str, Any]) -> Any:
    return {"answer": f"Hello, about {task['message']}"}


@dataclass
class Api:
    http: httpx2.AsyncClient
    runtime: LocalRuntime

    async def bearer(self, *roles: str) -> dict[str, str]:
        _, value = await self.runtime.runtime.create_key(
            Identity("system:platform", "acme"), "-".join(roles), list(roles)
        )
        return {"Authorization": f"Bearer {value}"}

    async def done(self, experiment: str, headers: dict[str, str]) -> dict[str, Any]:
        async with asyncio.timeout(30):
            while True:
                found = await self.http.get(
                    f"/v1/apps/desk/experiments/{experiment}", headers=headers
                )
                body: dict[str, Any] = found.json()
                if body["status"] == "completed":
                    return body
                await asyncio.sleep(0.05)


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    module = ModuleType("eval_api_desk")
    module.answer = _answer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "eval_api_desk", module)
    async with LocalRuntime(tenant="acme") as runtime:
        await runtime.runtime.apply(load_manifests(DESK), tenant="acme")
        app = create_app(runtime.runtime, authenticator=ApiKeys(runtime.runtime.security))
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
            yield Api(http, runtime)


async def test_an_operator_runs_a_suite_and_anyone_reads_it(api: Api) -> None:
    operator = await api.bearer("operator")
    started = await api.http.post(
        "/v1/apps/desk/experiments", headers=operator, json={"suite": SUITE, "label": "first"}
    )
    assert started.status_code == 202, started.text
    experiment = started.json()["experiment"]
    found = await api.done(experiment, await api.bearer("auditor"))
    assert found["label"] == "first"
    assert found["pending"] == 1
    assert found["gate_result"]["passed"] is False
    listed = (
        await api.http.get("/v1/apps/desk/experiments", headers=await api.bearer("member"))
    ).json()
    assert [e["experiment"] for e in listed] == [experiment]


async def test_a_member_cant_run_one(api: Api) -> None:
    refused = await api.http.post(
        "/v1/apps/desk/experiments", headers=await api.bearer("member"), json={"suite": SUITE}
    )
    assert refused.status_code == 403


async def test_a_reviewer_grades_and_nobody_else(api: Api) -> None:
    operator = await api.bearer("operator")
    started = await api.http.post(
        "/v1/apps/desk/experiments", headers=operator, json={"suite": SUITE}
    )
    experiment = started.json()["experiment"]
    await api.done(experiment, operator)
    path = f"/v1/apps/desk/experiments/{experiment}/grades"
    body = {"case": "hi", "grader": "tone", "score": 1.0, "comment": "warm"}
    assert (
        await api.http.post(path, headers=await api.bearer("member"), json=body)
    ).status_code == 403
    assert (
        await api.http.post(path, headers=await api.bearer("auditor"), json=body)
    ).status_code == 403
    # named by the grader, but an auditor changes nothing
    auditing = await api.http.post(path, headers=await api.bearer("auditor", "qa"), json=body)
    assert auditing.status_code == 403
    graded = await api.http.post(path, headers=await api.bearer("member", "qa"), json=body)
    assert graded.status_code == 200, graded.text
    assert graded.json()["gate_result"]["passed"] is True
    again = await api.http.post(path, headers=await api.bearer("member", "qa"), json=body)
    assert again.status_code == 409
    wrong = {**body, "grader": "polite"}
    assert (
        await api.http.post(path, headers=await api.bearer("member", "qa"), json=wrong)
    ).status_code == 404


async def test_what_isnt_a_suite_is_refused(api: Api) -> None:
    operator = await api.bearer("operator")
    bad: dict[str, Any] = {**SUITE, "spec": {"cases": []}}
    assert (
        await api.http.post("/v1/apps/desk/experiments", headers=operator, json={"suite": bad})
    ).status_code == 422
    other = {**SUITE, "spec": {**SUITE["spec"], "app": "elsewhere"}}
    assert (
        await api.http.post("/v1/apps/desk/experiments", headers=operator, json={"suite": other})
    ).status_code == 409
    assert (
        await api.http.post("/v1/apps/nope/experiments", headers=operator, json={"suite": SUITE})
    ).status_code == 404


async def test_two_experiments_compared(api: Api) -> None:
    operator = await api.bearer("operator")
    ids: list[str] = []
    for _ in range(2):
        started = await api.http.post(
            "/v1/apps/desk/experiments", headers=operator, json={"suite": SUITE}
        )
        ids.append(started.json()["experiment"])
        await api.done(ids[-1], operator)
    found = await api.http.get(
        f"/v1/apps/desk/experiments/{ids[0]}/compare/{ids[1]}", headers=operator
    )
    assert found.status_code == 200
    assert found.json()["cases"][0]["change"] == 0.0
    missing = await api.http.get(
        f"/v1/apps/desk/experiments/{ids[0]}/compare/{UNKNOWN}", headers=operator
    )
    assert missing.status_code == 404
    assert (
        await api.http.get(f"/v1/apps/desk/experiments/{UNKNOWN}", headers=operator)
    ).status_code == 404


async def test_online_grades_are_listed(api: Api) -> None:
    found = await api.http.get(
        "/v1/apps/desk/evaluations/online", headers=await api.bearer("auditor")
    )
    assert found.status_code == 200
    assert found.json() == []

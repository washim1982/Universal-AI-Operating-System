"""Replay, fork, retry and compare over HTTP (FR-OBS-04, FR-WF-11), and who may do which."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.api import create_app
from aios.core.identity import Identity
from aios.core.manifests import load_manifests
from aios.security.identity import ApiKeys
from aios.testing.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "apps" / "ai-researcher"
CASSETTES = ROOT / "tests" / "cassettes" / "ai-researcher"
NAME = "ai-researcher"
QUESTION = "What changed in the OpenTelemetry GenAI semantic conventions in 2026?"
UNKNOWN = "01J0000000000000000000000A"
WAITER = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: waiter, version: 0.1.0}
spec:
  entrypoints: [{type: chat, workflow: wait}]
  workflows: [wait]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: wait, version: 0.1.0}
spec:
  steps:
    - {id: reply, type: wait, signal: reply}
"""


@dataclass
class Api:
    http: httpx2.AsyncClient
    runtime: LocalRuntime
    run: str

    async def bearer(self, *roles: str) -> dict[str, str]:
        _, value = await self.runtime.runtime.create_key(
            Identity("system:platform", "acme"), "-".join(roles), list(roles)
        )
        return {"Authorization": f"Bearer {value}"}

    async def finished(self, run: str, headers: dict[str, str], app: str = NAME) -> dict[str, Any]:
        async with asyncio.timeout(30):
            while True:
                view = (await self.http.get(f"/v1/apps/{app}/runs/{run}", headers=headers)).json()
                if view["status"] not in ("running", "paused"):
                    return view
                await asyncio.sleep(0.05)


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, tenants=["acme"]) as runtime:
        app = create_app(runtime.runtime, authenticator=ApiKeys(runtime.runtime.security))
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
            first = Api(http, runtime, "")
            member = await first.bearer("member")
            started = await http.post(
                f"/v1/apps/{NAME}/runs", headers=member, json={"message": QUESTION}
            )
            first.run = started.json()["run_id"]
            await first.finished(first.run, member)
            yield first


async def test_a_replay_runs_again_from_the_record(api: Api) -> None:
    member = await api.bearer("member")
    started = await api.http.post(f"/v1/apps/{NAME}/runs/{api.run}/replay", headers=member)
    assert started.status_code == 202, started.text
    new = started.json()["run_id"]
    view = await api.finished(new, member)
    original = await api.finished(api.run, member)
    assert view["status"] == "completed"
    assert view["output"] == original["output"]
    assert view["usage"]["cost_usd"] == 0
    assert (view["source"]["run"], view["source"]["mode"]) == (api.run, "replay")


async def test_a_fork_needs_a_step(api: Api) -> None:
    member = await api.bearer("member")
    path = f"/v1/apps/{NAME}/runs/{api.run}/fork"
    assert (await api.http.post(path, headers=member, json={})).status_code == 422
    unknown = await api.http.post(path, headers=member, json={"step": "nope"})
    assert unknown.status_code == 404
    model = await api.http.post(path, headers=member, json={"step": "review", "model": "x/y"})
    assert model.status_code == 404


async def test_a_fork_starts_from_its_step(api: Api) -> None:
    member = await api.bearer("member")
    body = {"step": "review", "model": "anthropic/claude-haiku-5-5"}
    started = await api.http.post(f"/v1/apps/{NAME}/runs/{api.run}/fork", headers=member, json=body)
    assert started.status_code == 202, started.text
    new = started.json()["run_id"]
    view = await api.finished(new, member)
    assert view["status"] == "completed"
    assert view["source"]["step"] == "review"
    assert view["source"]["copied"] == ["plan", "research", "analyze", "draft"]
    core = api.runtime.runtime
    kept = await core.run_state(NAME, api.run, tenant="acme")
    assert (await core.run_state(NAME, new, tenant="acme")).input == kept.input  # left out: kept


async def test_a_retry_needs_someone_who_controls_runs(api: Api) -> None:
    """A retry does things for real again, so starting runs isn't enough."""
    path = f"/v1/apps/{NAME}/runs/{api.run}/retry"
    member = await api.http.post(path, headers=await api.bearer("member"), json={"step": "review"})
    assert member.status_code == 403
    operator = await api.bearer("operator")
    allowed = await api.http.post(path, headers=operator, json={"step": "review"})
    assert allowed.status_code == 202, allowed.text


async def test_a_retry_of_a_run_that_didnt_fail_says_so(api: Api) -> None:
    operator = await api.bearer("operator")
    found = await api.http.post(f"/v1/apps/{NAME}/runs/{api.run}/retry", headers=operator)
    assert found.status_code == 409
    assert "nothing failed" in found.text


async def test_a_run_still_going_cant_be_replayed(api: Api) -> None:
    await api.runtime.runtime.apply(load_manifests(WAITER), tenant="acme")
    member = await api.bearer("member")
    started = await api.http.post("/v1/apps/waiter/runs", headers=member, json={"message": "x"})
    run = started.json()["run_id"]
    async with asyncio.timeout(10):
        while True:
            view = (await api.http.get(f"/v1/apps/waiter/runs/{run}", headers=member)).json()
            if view["status"] == "paused":
                break
            await asyncio.sleep(0.02)
    again = await api.http.post(f"/v1/apps/waiter/runs/{run}/replay", headers=member)
    assert again.status_code == 409


async def test_replaying_nothing(api: Api) -> None:
    member = await api.bearer("member")
    path = f"/v1/apps/{NAME}/runs/{UNKNOWN}/replay"
    assert (await api.http.post(path, headers=member)).status_code == 404
    other = await api.http.post(f"/v1/apps/nope/runs/{api.run}/replay", headers=member)
    assert other.status_code == 404


async def test_two_runs_compared(api: Api) -> None:
    member = await api.bearer("member")
    started = await api.http.post(f"/v1/apps/{NAME}/runs/{api.run}/replay", headers=member)
    new = started.json()["run_id"]
    await api.finished(new, member)
    found = await api.http.get(f"/v1/apps/{NAME}/runs/{api.run}/compare/{new}", headers=member)
    assert found.status_code == 200
    body = found.json()
    assert body["same_output"]
    assert [step["step"] for step in body["steps"]] == [
        "plan",
        "research",
        "analyze",
        "draft",
        "review",
    ]
    missing = await api.http.get(
        f"/v1/apps/{NAME}/runs/{api.run}/compare/{UNKNOWN}", headers=member
    )
    assert missing.status_code == 404


async def test_the_run_list_says_where_a_run_came_from(api: Api) -> None:
    member = await api.bearer("member")
    started = await api.http.post(f"/v1/apps/{NAME}/runs/{api.run}/replay", headers=member)
    new = started.json()["run_id"]
    await api.finished(new, member)
    listed = (await api.http.get(f"/v1/apps/{NAME}/runs", headers=member)).json()
    [found] = [run for run in listed if run["run_id"] == new]
    assert (found["source_run"], found["mode"]) == (api.run, "replay")

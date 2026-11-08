"""People as capability providers (FR-HUM-08; WP 3.5)."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import httpx2
import pytest

from aios.api import create_app
from aios.core.errors import ConflictError, NotFoundError, PolicyDeniedError
from aios.core.human import HumanTask
from aios.core.identity import Identity
from aios.core.manifests import load_manifests
from aios.fabric import Scope
from aios.security.identity import ApiKeys
from aios.testing.runtime import LocalRuntime, RunHandle

DESK = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: contract}]
  workflows: [contract]
  humans:
    - capability: legal.review
      assignees: ["group:legal", "role:counsel"]
      description: Read the contract and say whether it can be signed.
      output:
        type: object
        required: [verdict]
        properties:
          verdict: {enum: [sign, change]}
          notes: {type: string}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: contract, version: 1.0.0}
spec:
  output: "${review.output}"
  steps:
    - {id: review, type: task, capability: legal.review, input: {contract: "nda-7"}}
"""
ANN = Identity("user:ann@acme", "acme", frozenset({"member"}))
CARL = Identity("user:carl@acme", "acme", frozenset({"member", "counsel"}))
EVE = Identity("user:eve@acme", "acme", frozenset({"member"}))
ADMIN = Identity("system:platform", "acme")


@dataclass
class Desk:
    runtime: LocalRuntime

    async def assigned(self) -> tuple[RunHandle, HumanTask]:
        handle = await self.runtime.chat("desk", "review it")
        event = await handle.wait_for_event("human.task.assigned", timeout=10)
        data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
        human = self.runtime.runtime.human
        async with asyncio.timeout(10):
            while True:
                if (found := await human.task("acme", str(data["task"]))) is not None:
                    return handle, found
                await asyncio.sleep(0.02)

    async def events(self, run: str, type: str) -> list[dict[str, Any]]:
        bus = self.runtime.runtime.bus
        found = [s.event async for s in bus.replay(Scope("acme", "desk"), type, run_id=run)]
        return [e.data for e in found if isinstance(e.data, dict)]


@pytest.fixture
async def desk() -> AsyncIterator[Desk]:
    async with LocalRuntime(tenant="acme") as runtime:
        await runtime.runtime.apply(load_manifests(DESK), tenant="acme")
        await runtime.runtime.set_group(ADMIN, "legal", ["user:ann@acme"])
        yield Desk(runtime)


async def test_people_do_a_capabilitys_work_and_the_run_goes_on(desk: Desk) -> None:
    core = desk.runtime.runtime
    handle, task = await desk.assigned()
    assert task.details.capability == "legal.review"
    assert task.details.input == {"contract": "nda-7"}
    assert task.details.description.startswith("Read the contract")
    assert [t.id for t in await core.human.assigned(ANN)] == [task.id]
    assert [t.id for t in await core.human.assigned(CARL)] == [task.id]  # by role
    assert await core.human.assigned(EVE) == []
    [claimed] = await desk.events(handle.run_id, "task.claimed")
    assert claimed["agent"] == "people"

    done = await core.complete_task(
        ANN, task.id, {"verdict": "sign", "notes": "standard terms"}, comment="read it twice"
    )
    assert (done.status, done.by) == ("completed", "user:ann@acme")
    result = await handle.wait(timeout=10)
    assert result.status == "completed"
    assert result.output == {"verdict": "sign", "notes": "standard terms"}
    [completed] = await desk.events(handle.run_id, "human.task.completed")
    assert (completed["by"], completed["comment"]) == ("user:ann@acme", "read it twice")
    assert await core.human.assigned(ANN) == []


async def test_an_answer_must_fit_what_was_asked(desk: Desk) -> None:
    core = desk.runtime.runtime
    _, task = await desk.assigned()
    with pytest.raises(ValueError, match="doesn't fit"):
        await core.complete_task(ANN, task.id, {"verdict": "maybe"})
    with pytest.raises(ValueError, match="doesn't fit"):
        await core.complete_task(ANN, task.id, "sign it")
    assert (await core.human.task("acme", task.id)).status == "pending"  # type: ignore[union-attr]


async def test_only_an_assignee_does_it_and_only_once(desk: Desk) -> None:
    core = desk.runtime.runtime
    handle, task = await desk.assigned()
    with pytest.raises(PolicyDeniedError, match="isn't for this caller"):
        await core.complete_task(EVE, task.id, {"verdict": "sign"})
    await core.complete_task(CARL, task.id, {"verdict": "change"})
    with pytest.raises(ConflictError, match="already completed"):
        await core.complete_task(ANN, task.id, {"verdict": "sign"})
    with pytest.raises(NotFoundError):
        await core.complete_task(ANN, "01J0000000000000000000000A", {"verdict": "sign"})
    assert (await handle.wait(timeout=10)).output == {"verdict": "change"}


async def test_when_the_run_ends_the_work_is_withdrawn(desk: Desk) -> None:
    core = desk.runtime.runtime
    handle, task = await desk.assigned()
    await core.cancel("desk", handle.run_id, tenant="acme")
    async with asyncio.timeout(10):
        while True:
            found = await core.human.task("acme", task.id)
            if found is not None and found.status == "cancelled":
                break
            await asyncio.sleep(0.02)
    [cancelled] = await desk.events(handle.run_id, "human.task.cancelled")
    assert cancelled["reason"] == "run.cancelled"
    assert await core.human.assigned(ANN) == []


async def _review(ctx: Any, task: Any) -> Any:
    return {"verdict": "sign"}


async def test_a_capability_is_peoples_or_an_agents_not_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("tasks_lawyer")
    module.review = _review  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tasks_lawyer", module)
    both = DESK.replace(
        "  workflows: [contract]\n", "  workflows: [contract]\n  agents: [lawyer]\n"
    ) + (
        "---\napiVersion: aios/v1alpha1\nkind: Agent\nmetadata: {name: lawyer, version: 1.0.0}\n"
        'spec:\n  runtime: {type: python, entrypoint: "tasks_lawyer:review"}\n'
        "  capabilities: [{name: legal.review}]\n"
    )
    async with LocalRuntime(tenant="acme") as runtime:
        with pytest.raises(ValueError, match="both an agent's and people's"):
            await runtime.runtime.apply(load_manifests(both), tenant="acme")


def test_a_capability_is_named_once() -> None:
    twice = DESK.replace(
        "  humans:\n",
        '  humans:\n    - {capability: legal.review, assignees: ["role:counsel"]}\n',
    )
    with pytest.raises(ValueError, match="twice"):
        load_manifests(twice)


# ---------------------------------------------------------------------- over HTTP


async def test_work_is_listed_and_done_over_http(desk: Desk) -> None:
    core = desk.runtime.runtime
    app = create_app(core, authenticator=ApiKeys(core.security))
    _, ann_key = await core.create_key(ADMIN, "ann", ["member"])
    _, op_key = await core.create_key(ADMIN, "ops", ["operator"])
    _, aud_key = await core.create_key(ADMIN, "audit", ["auditor"])
    await core.set_group(ADMIN, "legal", ["service:ann@acme"])
    ann = {"Authorization": f"Bearer {ann_key}"}
    handle, task = await desk.assigned()
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
        listed = (await http.get("/v1/tasks", headers=ann)).json()
        assert [t["task"] for t in listed] == [task.id]
        assert listed[0]["output_schema"]["required"] == ["verdict"]
        assert listed[0]["trace"] == f"/v1/apps/desk/runs/{handle.run_id}/events"
        ops = {"Authorization": f"Bearer {op_key}"}
        assert (await http.get("/v1/tasks", headers=ops)).json() == []  # not theirs
        assert len((await http.get("/v1/tasks?everyone=true", headers=ops)).json()) == 1
        assert (await http.get("/v1/tasks?everyone=true", headers=ann)).status_code == 403
        auditor = {"Authorization": f"Bearer {aud_key}"}
        assert (await http.get("/v1/tasks", headers=auditor)).status_code == 403
        assert (await http.get(f"/v1/tasks/{task.id}", headers=ops)).status_code == 404
        bad = await http.post(
            f"/v1/tasks/{task.id}/complete", headers=ann, json={"output": {"verdict": "?"}}
        )
        assert bad.status_code == 422
        stranger = await http.post(
            f"/v1/tasks/{task.id}/complete", headers=ops, json={"output": {"verdict": "sign"}}
        )
        assert stranger.status_code == 403
        done = await http.post(
            f"/v1/tasks/{task.id}/complete", headers=ann, json={"output": {"verdict": "sign"}}
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "completed"
        again = await http.post(
            f"/v1/tasks/{task.id}/complete", headers=ann, json={"output": {"verdict": "sign"}}
        )
        assert again.status_code == 409
    assert (await handle.wait(timeout=10)).status == "completed"


def test_the_cli_lists_shows_and_completes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from aios.cli import client as cli_client  # noqa: PLC0415
    from aios.cli.main import main  # noqa: PLC0415

    seen: list[httpx2.Request] = []
    original = httpx2.Client

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"status": "completed"})

    def client(**kwargs: Any) -> httpx2.Client:
        return original(transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", client)
    task = "01J0000000000000000000000A"
    assert main(["tasks", "list"]) == 0
    assert main(["tasks", "show", task]) == 0
    assert (
        main(["tasks", "complete", task, "--output", '{"verdict":"sign"}', "--comment", "ok"]) == 0
    )
    assert main(["tasks", "complete", task, "--output", "looks fine"]) == 0
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/v1/tasks"),
        ("GET", f"/v1/tasks/{task}"),
        ("POST", f"/v1/tasks/{task}/complete"),
        ("POST", f"/v1/tasks/{task}/complete"),
    ]
    assert json.loads(seen[2].content) == {"output": {"verdict": "sign"}, "comment": "ok"}
    assert json.loads(seen[3].content) == {"output": "looks fine", "comment": None}
    assert main(["tasks", "complete", task, "--field", "verdict=sign", "--field", "pages=3"]) == 0
    assert json.loads(seen[4].content)["output"] == {"verdict": "sign", "pages": 3}
    with pytest.raises(SystemExit, match="--output or --field"):
        main(["tasks", "complete", task])
    with pytest.raises(SystemExit, match="not both"):
        main(["tasks", "complete", task, "--output", "x", "--field", "a=1"])
    with pytest.raises(SystemExit, match="NAME=VALUE"):
        main(["tasks", "complete", task, "--field", "verdict"])
    with pytest.raises(SystemExit, match="task id"):
        main(["tasks", "show"])

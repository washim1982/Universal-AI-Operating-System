"""Feedback on runs, feeding evaluations and memory (FR-HUM-09; WP 3.5)."""

import json
import sys
from collections.abc import AsyncIterator
from types import ModuleType
from typing import Any

import httpx2
import pytest
import yaml
from pydantic import JsonValue

from aios.api import create_app
from aios.core.catalog import FeedbackGiven
from aios.core.errors import NotFoundError, PolicyDeniedError
from aios.core.identity import Identity
from aios.core.manifests import EvaluationManifest, load_manifests
from aios.fabric import Scope
from aios.kernel import Context
from aios.security.identity import ApiKeys
from aios.testing.runtime import LocalRuntime

DESK = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: answer}]
  workflows: [answer]
  agents: [writer]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: writer, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "feedback_desk:writer"}
  capabilities: [{name: desk.answer}]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: answer, version: 1.0.0}
spec:
  output: "${write.output}"
  steps:
    - {id: write, type: task, capability: desk.answer, input: {q: "${input.message}"}}
"""
ANN = Identity("user:ann@acme", "acme", frozenset({"member"}))
ADMIN = Identity("system:platform", "acme")


async def writer(ctx: Context, task: Any) -> Any:
    return {"answer": "Paris is in Germany"}


@pytest.fixture
async def desk(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[LocalRuntime, str]]:
    module = ModuleType("feedback_desk")
    module.writer = writer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "feedback_desk", module)
    async with LocalRuntime(tenant="acme") as runtime:
        await runtime.runtime.apply(load_manifests(DESK), tenant="acme")
        await runtime.runtime.security.set_roles("acme", ANN.principal, ANN.roles)
        handle = await runtime.chat("desk", "Where is Paris?")
        assert (await handle.wait(timeout=10)).status == "completed"
        yield runtime, handle.run_id


async def test_a_correction_is_on_the_run_and_becomes_an_eval_case(
    desk: tuple[LocalRuntime, str],
) -> None:
    runtime, run = desk
    core = runtime.runtime
    correction: dict[str, JsonValue] = {"answer": "Paris is in France"}
    given = FeedbackGiven(by=ANN.principal, rating="bad", correction=correction, comment="wrong")
    found = await core.give_feedback(ANN, "desk", run, given)
    assert found.memory is None
    bus = core.bus
    events = [s.event async for s in bus.replay(Scope("acme", "desk"), run_id=run)]
    [feedback] = [e for e in events if e.type == "human.feedback.given"]
    assert feedback.principal == ANN.principal
    assert isinstance(feedback.data, dict)
    assert (feedback.data["rating"], feedback.data["correction"]) == ("bad", correction)
    listed = await core.feedback("acme", "desk", run_id=run)
    assert [f.event.id for f in listed] == [feedback.id]

    [case] = await core.feedback_cases("acme", "desk")
    assert case.input == {"message": "Where is Paris?"}
    assert case.expected == correction
    assert case.tags == ["feedback"]
    # it fits straight into an Evaluation
    manifest = {
        "apiVersion": "aios/v1alpha1",
        "kind": "Evaluation",
        "metadata": {"name": "from-feedback", "version": "0.1.0"},
        "spec": {
            "app": "desk",
            "cases": [case.model_dump(mode="json")],
            "graders": [{"name": "same", "type": "exact"}],
        },
    }
    EvaluationManifest.model_validate(manifest)


async def test_a_step_can_be_rated_but_its_correction_isnt_a_case(
    desk: tuple[LocalRuntime, str],
) -> None:
    runtime, run = desk
    core = runtime.runtime
    given = FeedbackGiven(by=ANN.principal, step="write", correction={"answer": "France"})
    await core.give_feedback(ANN, "desk", run, given)
    assert await core.feedback_cases("acme", "desk") == []
    with pytest.raises(ValueError, match="no step"):
        await core.give_feedback(
            ANN, "desk", run, FeedbackGiven(by=ANN.principal, step="nope", rating="good")
        )
    with pytest.raises(NotFoundError):
        await core.give_feedback(
            ANN,
            "desk",
            "01J0000000000000000000000A",
            FeedbackGiven(by=ANN.principal, rating="good"),
        )


async def test_feedback_needs_its_permission(desk: tuple[LocalRuntime, str]) -> None:
    runtime, run = desk
    nobody = Identity("user:nobody@acme", "acme", frozenset())
    with pytest.raises(PolicyDeniedError, match=r"run.feedback"):
        await runtime.runtime.give_feedback(
            nobody, "desk", run, FeedbackGiven(by=nobody.principal, rating="bad")
        )
    assert await runtime.runtime.feedback("acme", "desk") == []


def test_feedback_says_something() -> None:
    with pytest.raises(ValueError, match="rating, a correction or a comment"):
        FeedbackGiven(by=ANN.principal)


async def test_remembered_feedback_is_in_the_apps_memory(
    desk: tuple[LocalRuntime, str],
) -> None:
    runtime, run = desk
    core = runtime.runtime
    given = FeedbackGiven(
        by=ANN.principal,
        rating="bad",
        correction={"answer": "Paris is in France"},
        comment="Check the country before answering.",
        remember=True,
    )
    found = await core.give_feedback(ANN, "desk", run, given)
    assert found.memory is not None
    [item] = await core.memory.of_app("acme", "desk")
    assert item.id == found.memory
    assert (item.scope, item.owner, item.kind) == ("app", "desk", "episodic")
    assert "Check the country before answering." in item.content
    assert "Paris is in France" in item.content
    assert item.provenance.author == ANN.principal
    assert item.metadata["feedback"] == found.event.id


async def test_feedback_over_http_and_from_the_cli(
    desk: tuple[LocalRuntime, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, run = desk
    core = runtime.runtime
    app = create_app(core, authenticator=ApiKeys(core.security))
    _, member = await core.create_key(ADMIN, "ann", ["member"])
    _, auditor = await core.create_key(ADMIN, "audit", ["auditor"])
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
        path = f"/v1/apps/desk/runs/{run}/feedback"
        headers = {"Authorization": f"Bearer {member}"}
        given = await http.post(
            path, headers=headers, json={"rating": "bad", "correction": {"answer": "France"}}
        )
        assert given.status_code == 201, given.text
        assert given.json()["by"] == "service:ann@acme"
        assert (await http.post(path, headers=headers, json={})).status_code == 422
        no_run = path.replace(run, "01J0000000000000000000000A")
        assert (
            await http.post(no_run, headers=headers, json={"rating": "good"})
        ).status_code == 404
        read_only = {"Authorization": f"Bearer {auditor}"}
        assert (
            await http.post(path, headers=read_only, json={"rating": "good"})
        ).status_code == 403
        listed = await http.get("/v1/apps/desk/feedback", headers=read_only)
        assert [f["rating"] for f in listed.json()] == ["bad"]
        cases = (await http.get("/v1/apps/desk/feedback/cases", headers=read_only)).json()
        assert cases[0]["expected"] == {"answer": "France"}
        assert "query" not in cases[0]  # only what a case says

    from aios.cli import client as cli_client  # noqa: PLC0415
    from aios.cli.main import main  # noqa: PLC0415

    seen: list[httpx2.Request] = []
    original = httpx2.Client

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path.endswith("/cases"):
            return httpx2.Response(200, json=cases)
        return httpx2.Response(200, json={"ok": True})

    def client(**kwargs: Any) -> httpx2.Client:
        return original(transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", client)
    assert (
        main(["feedback", "give", "desk", run, "--rating", "good", "--correction", "France"]) == 0
    )
    assert json.loads(seen[0].content) == {
        "rating": "good",
        "step": None,
        "comment": None,
        "remember": False,
        "correction": "France",
    }
    capsys.readouterr()
    assert main(["feedback", "cases", "desk"]) == 0
    printed = yaml.safe_load(capsys.readouterr().out)
    assert printed["cases"][0]["expected"] == {"answer": "France"}
    assert main(["feedback", "list", "desk", run]) == 0
    assert seen[-1].url.params["run"] == run
    assert main(["feedback", "give", "desk", run, "--correct", "answer=France"]) == 0
    assert json.loads(seen[-1].content)["correction"] == {"answer": "France"}
    with pytest.raises(SystemExit, match="not both"):
        main(["feedback", "give", "desk", run, "--correction", "x", "--correct", "a=1"])
    with pytest.raises(SystemExit, match="run's id"):
        main(["feedback", "give", "desk"])


async def test_remembering_needs_memory_write_as_well(desk: tuple[LocalRuntime, str]) -> None:
    """Feedback is ``run.feedback``; keeping it in the App's memory is a memory write, checked
    with the person's own roles."""
    runtime, run = desk
    core = runtime.runtime
    nobody = Identity("user:nobody@acme", "acme", frozenset())
    given = FeedbackGiven(by=nobody.principal, comment="remember this", remember=True)
    event = (await core.feedback("acme", "desk")) or None
    assert event is None  # nothing given yet
    started = await core.run_state("desk", run, tenant="acme")
    assert started.started_event is not None
    with pytest.raises(PolicyDeniedError, match="memory"):
        await core._remember_feedback(nobody, started.started_event, given)  # pyright: ignore[reportPrivateUsage]
    assert await core.memory.of_app("acme", "desk") == []

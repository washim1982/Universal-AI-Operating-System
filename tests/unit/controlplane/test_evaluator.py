"""The evaluator (FR-OBS-07, FR-MEM-12; M3 exit 3): experiments, judges, people, online."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import JsonValue

from aios.core.envelope import Event
from aios.core.errors import ConflictError, PolicyDeniedError
from aios.core.evals import Experiment, check_gate
from aios.core.identity import Identity
from aios.core.manifests import EvaluationManifest, load_evaluation, load_manifests
from aios.core.ports import ModelRequest
from aios.fabric import Scope
from aios.kernel import Context
from aios.models import Completion, ModelEntry, Provider, ProviderError
from aios.testing.runtime import LocalRuntime, RunHandle

ROOT = Path(__file__).resolve().parents[3]
RESEARCHER = ROOT / "apps" / "ai-researcher"
CASSETTES = ROOT / "tests" / "cassettes" / "ai-researcher"
SUITE = RESEARCHER / "evals" / "quality.yaml"
BASELINE = RESEARCHER / "evals" / "baseline.json"
KB = ROOT / "tests" / "fixtures" / "kb"

DESK = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: answer}]
  agents: [answerer]
  workflows: [answer]
  modelPolicy: judging
  memory: {scopes: [app]}
  policies: [desk-reads]
%s
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: answerer, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "eval_desk:answer"}
  capabilities: [{name: desk.answer}]
  memory: {read: [app]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: answer, version: 1.0.0}
spec:
  steps:
    - {id: answer, type: task, capability: desk.answer, input: {message: "${input.message}"}}
---
apiVersion: aios/v1alpha1
kind: ModelPolicy
metadata: {name: judging, version: 1.0.0}
spec:
  default: {candidates: [fake/judge]}
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: desk-reads, version: 1.0.0}
spec:
  subject: {app: desk}
  allow:
    - {action: memory.read, scopes: [app]}
"""
WAITER = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: waiter, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: wait}]
  workflows: [wait]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: wait, version: 1.0.0}
spec:
  steps:
    - {id: reply, type: wait, signal: reply}
"""


def suite(spec: dict[str, Any], app: str = "desk") -> EvaluationManifest:
    return EvaluationManifest.model_validate(
        {"metadata": {"name": "checks", "version": "1.0.0"}, "spec": {"app": app, **spec}}
    )


async def finished(runtime: LocalRuntime, app: str, experiment: str) -> Experiment:
    core = runtime.runtime
    async with asyncio.timeout(60):
        while True:
            found = await core.evaluator.experiment(core.tenant, app, experiment)
            if found is not None and found.status == "completed":
                return found
            await asyncio.sleep(0.05)


async def evaluate(runtime: LocalRuntime, manifest: EvaluationManifest, app: str) -> Experiment:
    core = runtime.runtime
    experiment = await core.evaluator.request(
        core.tenant, app, manifest, principal="user:anonymous", on_behalf_of=None
    )
    return await finished(runtime, app, experiment)


async def events(runtime: LocalRuntime, app: str, run: str, pattern: str = ">") -> list[Event]:
    scope = Scope(runtime.runtime.tenant, app)
    return [s.event async for s in runtime.runtime.bus.replay(scope, pattern, run_id=run)]


# --------------------------------------------------------------------------- the researcher


@pytest.fixture
async def researcher(tmp_path: Path) -> AsyncIterator[Callable[[Path], Awaitable[LocalRuntime]]]:
    running: list[LocalRuntime] = []

    async def make(cassettes: Path = CASSETTES) -> LocalRuntime:
        # Durable: on PostgreSQL when AIOS_TEST_DATABASE_URL is set.
        runtime = LocalRuntime.from_app(RESEARCHER, cassettes=cassettes, durable=True)
        await runtime.__aenter__()
        running.append(runtime)
        return runtime

    yield make
    for runtime in running:
        await runtime.__aexit__(None, None, None)


async def test_a_suite_runs_each_case_as_a_run_and_grades_it(researcher: Any) -> None:
    runtime = await researcher()
    found = await evaluate(runtime, load_evaluation(SUITE), "ai-researcher")
    [case] = found.cases
    assert case.status == "completed"
    assert case.run is not None
    assert (await RunHandle(runtime, "ai-researcher", case.run).wait()).status == "completed"
    assert {g.grader: g.passed for g in case.grades} == {
        "report-shape": True,
        "cites-the-spec": True,
        "on-topic": True,
        "grounded": False,
    }
    assert found.score == pytest.approx(0.828571, abs=1e-5)
    assert found.app_version == "0.1.0"
    assert found.workflow_version is not None
    assert found.cost_usd > 0
    assert found.drifted == 5  # the recording predates five of today's prompts


async def test_the_researcher_passes_its_gate_against_its_baseline(researcher: Any) -> None:
    runtime = await researcher()
    manifest = load_evaluation(SUITE)
    found = await evaluate(runtime, manifest, "ai-researcher")
    baseline = Experiment.from_summary(json.loads(BASELINE.read_text("utf-8")))
    assert check_gate(found, manifest.spec.gate, baseline).passed


async def test_a_change_that_scores_lower_is_blocked(researcher: Any, tmp_path: Path) -> None:
    """M3 exit 3: a prompt change is graded on what it now answers, recorded with a model.
    Here that recording cites a blog instead of the spec, and the gate blocks it."""
    recorded = json.loads((CASSETTES / "cassette.json").read_text("utf-8"))
    for interaction in recorded["interactions"]:
        if interaction["site"].startswith("draft/"):
            text = interaction["response"]["text"]
            interaction["response"]["text"] = text.replace(
                "https://opentelemetry.io/docs/specs/semconv/gen-ai/", "https://example.com/blog/"
            )
    worse = tmp_path / "worse"
    worse.mkdir()
    (worse / "cassette.json").write_text(json.dumps(recorded), "utf-8")
    runtime = await researcher(worse)
    manifest = load_evaluation(SUITE)
    found = await evaluate(runtime, manifest, "ai-researcher")
    baseline = Experiment.from_summary(json.loads(BASELINE.read_text("utf-8")))
    gate = check_gate(found, manifest.spec.gate, baseline)
    assert not gate.passed
    assert any("below the baseline" in reason for reason in gate.reasons), gate.reasons
    assert any(reason.startswith("cites-the-spec fell") for reason in gate.reasons)


async def test_a_stale_recording_blocks_a_suite_that_says_so(researcher: Any) -> None:
    runtime = await researcher()
    strict = load_evaluation(SUITE.read_text("utf-8").replace("onDrift: warn", "onDrift: fail"))
    found = await evaluate(runtime, strict, "ai-researcher")
    gate = check_gate(found, strict.spec.gate)
    assert not gate.passed
    assert any("record it again" in reason for reason in gate.reasons)


# --------------------------------------------------------------------------- a desk with a judge


class Judge(Provider):
    name = "fake"

    def __init__(self) -> None:
        self.asked: list[ModelRequest] = []
        self.down = False

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        self.asked.append(request)
        if self.down:
            raise ProviderError("the judge is down", retryable=False)
        return Completion(
            text='{"score": 8, "reason": "close"}',
            input_tokens=10,
            output_tokens=5,
            model=entry.model,
        )


ANSWERS: dict[str, Callable[[Context, dict[str, Any]], Awaitable[Any]]] = {}


async def _polite(ctx: Context, task: dict[str, Any]) -> Any:
    return {"answer": f"Hello! You asked: {task['message']}"}


async def _answer(ctx: Context, task: dict[str, Any]) -> Any:
    return await ANSWERS.get("now", _polite)(ctx, task)


Desk = tuple[LocalRuntime, Judge]


@pytest.fixture
async def desk(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Callable[..., Awaitable[Desk]]]:
    module = ModuleType("eval_desk")
    module.answer = _answer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "eval_desk", module)
    monkeypatch.setitem(ANSWERS, "now", _polite)
    running: list[LocalRuntime] = []

    async def make(online: str = "") -> Desk:
        judge = Judge()
        runtime = LocalRuntime(providers={"fake": judge})
        await runtime.__aenter__()
        running.append(runtime)
        runtime.runtime.models.registry.add(ModelEntry(id="fake/judge", model="judge"))
        await runtime.runtime.apply(load_manifests(DESK % online))
        await runtime.runtime.apply(load_manifests(WAITER))
        return runtime, judge

    yield make
    for runtime in running:
        await runtime.__aexit__(None, None, None)


JUDGED = {
    "cases": [{"id": "hi", "input": {"message": "hi"}, "expected": "a greeting"}],
    "graders": [
        {"name": "polite", "type": "contains", "path": "answer", "value": "hello"},
        {"name": "helpful", "type": "judge", "rubric": "Greets the user.", "weight": 2},
    ],
}


async def test_a_judge_grades_as_the_apps_evaluator(desk: Any) -> None:
    runtime, judge = await desk()
    found = await evaluate(runtime, suite(JUDGED), "desk")
    [case] = found.cases
    assert {g.grader: g.score for g in case.grades} == {"polite": 1.0, "helpful": 0.8}
    assert case.score == pytest.approx((1.0 + 0.8 * 2) / 3)
    [asked] = judge.asked
    assert '<untrusted source="the output">' in asked.messages[0].content
    assert "a greeting" in asked.messages[0].content  # the reference answer
    calls = await events(runtime, "desk", found.experiment, "model.call.completed")
    assert [c.data["agent"] for c in calls if isinstance(c.data, dict)] == ["aios-evaluator"]


async def test_a_judge_that_cant_answer_gives_nothing(desk: Any) -> None:
    runtime, judge = await desk()
    judge.down = True
    found = await evaluate(runtime, suite(JUDGED), "desk")
    helpful = next(g for g in found.cases[0].grades if g.grader == "helpful")
    assert helpful.score == 0.0
    assert "couldn't answer" in helpful.reason


HUMAN = {
    "cases": [{"id": "hi", "input": {"message": "hi"}}],
    "graders": [
        {"name": "polite", "type": "contains", "path": "answer", "value": "hello"},
        {"name": "tone", "type": "human", "reviewers": ["role:qa"], "question": "Is it warm?"},
    ],
}


async def test_a_person_grades_what_asks_for_one(desk: Any) -> None:
    runtime, _ = await desk()
    core = runtime.runtime
    found = await evaluate(runtime, suite(HUMAN), "desk")
    assert found.pending == 1
    gate = check_gate(found, found.gate)
    assert any("waiting for a person" in reason for reason in gate.reasons)
    stranger = Identity("user:bob@acme", core.tenant, frozenset({"member"}))
    with pytest.raises(PolicyDeniedError):
        await core.evaluator.grade(
            stranger, "desk", found.experiment, case="hi", grader="tone", score=1
        )
    reviewer = Identity("user:qa@acme", core.tenant, frozenset({"member", "qa"}))
    graded = await core.evaluator.grade(
        reviewer, "desk", found.experiment, case="hi", grader="tone", score=0.5, comment="cool"
    )
    tone = next(g for g in graded.cases[0].grades if g.grader == "tone")
    assert (tone.score, tone.by, tone.reason) == (0.5, "user:qa@acme", "cool")
    assert graded.pending == 0
    assert check_gate(graded, graded.gate).passed
    async with asyncio.timeout(10):  # a person's grade is on the audit record
        while True:
            entries = await core.audit.entries(core.tenant)
            if any(entry.event.type == "eval.case.graded" for entry in entries):
                break
            await asyncio.sleep(0.05)
    asked = [entry for entry in entries if entry.event.type == "eval.experiment.requested"]
    assert len(asked) == 1
    with pytest.raises(ConflictError):
        await core.evaluator.grade(
            reviewer, "desk", found.experiment, case="hi", grader="tone", score=1
        )


async def test_a_run_that_fails_scores_nothing(desk: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(ctx: Context, task: dict[str, Any]) -> Any:
        raise ValueError("no answer today")

    monkeypatch.setitem(ANSWERS, "now", broken)
    runtime, _ = await desk()
    found = await evaluate(runtime, suite(HUMAN), "desk")
    [case] = found.cases
    assert case.status == "failed"
    assert case.score == 0.0
    assert case.error is not None
    assert "no answer today" in case.error
    assert found.pending == 0  # nobody is asked to grade a run that didn't answer


async def test_a_run_that_never_ends_times_out(desk: Any) -> None:
    runtime, _ = await desk()
    waits = suite({**HUMAN, "timeout": "1s"}, app="waiter")
    found = await evaluate(runtime, waits, "waiter")
    [case] = found.cases
    assert case.status == "timeout"
    assert case.score == 0.0
    assert case.run is not None
    assert (await RunHandle(runtime, "waiter", case.run).wait(timeout=10)).status == "cancelled"


async def test_an_experiment_carries_on_where_it_stopped(desk: Any) -> None:
    """A worker that takes an experiment over starts no case twice."""
    runtime, _ = await desk()
    core = runtime.runtime
    await core.evaluator.stop()  # nobody takes the request: this test runs it by hand
    two = suite(
        {
            **JUDGED,
            "cases": [
                {"id": "a", "input": {"message": "a"}},
                {"id": "b", "input": {"message": "b"}},
            ],
        }
    )
    experiment = await core.evaluator.request(
        core.tenant, "desk", two, principal="user:anonymous", on_behalf_of=None
    )
    first = await core.evaluator.run(core.tenant, "desk", experiment)
    again = await core.evaluator.run(core.tenant, "desk", experiment)  # e.g. redelivered
    assert again.cases == first.cases
    asked = [e for e in await events(runtime, "desk", experiment, "eval.case.started")]
    assert len(asked) == 2


async def test_an_experiment_stopped_halfway_runs_only_the_rest(desk: Any) -> None:
    runtime, _ = await desk()
    core = runtime.runtime
    await core.evaluator.stop()
    cases = [{"id": "a", "input": {"message": "a"}}, {"id": "b", "input": {"message": "b"}}]
    two = suite({**JUDGED, "cases": cases})
    experiment = await core.evaluator.request(
        core.tenant, "desk", two, principal="user:anonymous", on_behalf_of=None
    )
    [asked] = await events(runtime, "desk", experiment)
    kept: JsonValue = {
        "case": "a",
        "status": "completed",
        "grades": [{"grader": "polite", "score": 0.25}],
    }
    steps: list[tuple[str, JsonValue]] = [
        ("eval.experiment.started", {"suite": "checks", "suite_version": "1.0.0", "cases": 2}),
        ("eval.case.completed", {"case": "a", "result": kept}),
    ]
    for type, data in steps:
        derived = asked.derive(type, data, source="test", principal="system:evaluator")
        await core.bus.publish(derived)
    found = await core.evaluator.run(core.tenant, "desk", experiment)
    assert [case.case for case in found.cases] == ["a", "b"]
    assert found.cases[0].score == 0.25  # kept from before the stop, not run again
    started = await events(runtime, "desk", experiment, "eval.case.started")
    assert [e.data["case"] for e in started if isinstance(e.data, dict)] == ["b"]


async def test_a_resumed_case_waits_for_the_run_it_had_started(desk: Any) -> None:
    runtime, _ = await desk()
    core = runtime.runtime
    await core.evaluator.stop()
    experiment = await core.evaluator.request(
        core.tenant, "desk", suite(JUDGED), principal="user:anonymous", on_behalf_of=None
    )
    [asked] = await events(runtime, "desk", experiment)
    run = await core.chat("desk", input={"message": "hi"})
    await core.bus.publish(
        asked.derive(
            "eval.case.started",
            {"case": "hi", "run": run},
            source="test",
            principal="system:evaluator",
        )
    )
    found = await core.evaluator.run(core.tenant, "desk", experiment)
    assert found.cases[0].run == run
    scope = Scope(core.tenant, "desk")
    runs = [s async for s in core.bus.replay(scope, "app.request.received")]
    assert len(runs) == 1


# --------------------------------------------------------------------------- retrieval

RETRIEVAL = {
    "retrieval": {"scopes": ["app"], "k": 2},
    "cases": [
        {
            "id": "reset",
            "query": "How do I reset my password?",
            "relevant": ["password-reset.md"],
            "answer": "Use the reset password link on the sign-in page.",
        },
        {
            "id": "returns",
            "query": "How long do I have to return an item?",
            "relevant": ["returns-policy.md"],
        },
    ],
}


async def test_retrieval_is_scored_on_a_golden_dataset(desk: Any) -> None:
    runtime, _ = await desk()
    await runtime.ingest_knowledge("desk", KB)
    found = await evaluate(runtime, suite(RETRIEVAL), "desk")
    metrics = found.metrics
    assert metrics["recall@2"] == 1.0
    assert metrics["mrr"] > 0.5
    assert 0 <= metrics["groundedness"] <= 1
    assert {case.status for case in found.cases} == {"retrieved"}


async def test_retrieval_reads_only_what_policy_lets_the_app_read(desk: Any) -> None:
    runtime, _ = await desk()
    await runtime.ingest_knowledge("desk", KB)
    tenant_wide = suite({**RETRIEVAL, "retrieval": {"scopes": ["tenant"], "k": 2}})
    found = await evaluate(runtime, tenant_wide, "desk")
    assert {case.status for case in found.cases} == {"error"}
    assert found.score == 0.0 or found.score is None


# --------------------------------------------------------------------------- online

ONLINE = """  evaluation:
    online:
      sample: %s
      graders:
        - {name: polite, type: contains, path: answer, value: hello}"""


async def test_a_sample_of_production_runs_is_graded_as_they_finish(desk: Any) -> None:
    runtime, _ = await desk(ONLINE % 1.0)
    handle = await runtime.chat("desk", "hi")
    await handle.wait()
    async with asyncio.timeout(10):
        while True:
            graded = await events(runtime, "desk", handle.run_id, "eval.run.graded")
            if graded:
                break
            await asyncio.sleep(0.05)
    [found] = graded
    assert isinstance(found.data, dict)
    assert found.data["score"] == 1.0
    core = runtime.runtime
    [listed] = await core.evaluator.online(core.tenant, "desk")
    assert listed["run"] == handle.run_id
    finished_event = next(
        e for e in await events(runtime, "desk", handle.run_id) if e.type == "run.completed"
    )
    await core.evaluator._on_run(finished_event)  # pyright: ignore[reportPrivateUsage] - redelivered
    assert len(await events(runtime, "desk", handle.run_id, "eval.run.graded")) == 1


async def test_a_run_outside_the_sample_isnt_graded(desk: Any) -> None:
    runtime, _ = await desk(ONLINE % 1e-12)
    handle = await runtime.chat("desk", "hi")
    await handle.wait()
    await asyncio.sleep(0.3)
    assert not await events(runtime, "desk", handle.run_id, "eval.run.graded")

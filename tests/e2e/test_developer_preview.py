"""M3 "Developer Preview": the demo's checks that no single other test makes whole.

* Exit 2 and the demo's middle: a bug seeded in a reference App is found in the Trace Explorer's
  view of the run, the fix is confirmed by forking the run from the faulty step, and the eval
  gate, which blocked the bug, turns green.
* Exit 6: a LangGraph agent runs as an AIOS agent, traced like any other, with its tools under
  AIOS policy: an approval pauses it, and a denial stops the tool, whatever the graph does with
  errors.

Exits 3, 4 and 5 have their own tests (``test_evaluator.py``, ``test_installer.py``,
``test_channels.py``), and exit 1 needs design partners.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from ai_researcher import agents as researcher_agents
from ai_researcher.agents import Report, WriteInput
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext
from pydantic import JsonValue

from aios import Context
from aios.controlplane import load_app, views
from aios.core.catalog import ApprovalRequested, ModelToolCall
from aios.core.evals import Experiment, check_gate
from aios.core.manifests import (
    ContainsGrader,
    EvaluationManifest,
    ExactGrader,
    RegexGrader,
    SchemaGrader,
    load_evaluation,
    load_manifests,
)
from aios.core.policy import SideEffect
from aios.core.ports import ModelRequest
from aios.evals.graders import Deterministic, deterministic
from aios.models import Completion, ModelEntry, Provider
from aios.observability import spans_for
from aios.observability.semconv import SUSPENDED
from aios.testing.runtime import LocalRuntime, RunHandle
from aios.tools import NativeProvider, tool

ROOT = Path(__file__).resolve().parents[2]
RESEARCHER = ROOT / "apps" / "ai-researcher"
CASSETTES = ROOT / "tests" / "cassettes" / "ai-researcher"
SUITE = RESEARCHER / "evals" / "quality.yaml"
BASELINE = RESEARCHER / "evals" / "baseline.json"
QUESTION = "What changed in the OpenTelemetry GenAI semantic conventions in 2026?"
APP = "ai-researcher"

# --------------------------------------------------------------------------- exit 2


FIXED = researcher_agents.write


async def write_one_source_per_site(ctx: Context, analysis: WriteInput) -> Report:
    """The writer with a seeded bug: a "dedupe" that keeps one source per site."""
    report = await FIXED(ctx, analysis)
    report.sources = sorted({urlsplit(url).netloc: url for url in report.sources}.values())
    return report


async def _graded(runtime: LocalRuntime, manifest: EvaluationManifest) -> Experiment:
    core = runtime.runtime
    experiment = await core.evaluator.request(
        core.tenant, APP, manifest, principal="user:anonymous", on_behalf_of=None
    )
    async with asyncio.timeout(60):
        while True:
            found = await core.evaluator.experiment(core.tenant, APP, experiment)
            if found is not None and found.status == "completed":
                return found
            await asyncio.sleep(0.05)


def _rules(manifest: EvaluationManifest) -> list[Deterministic]:
    rules = (ExactGrader, ContainsGrader, RegexGrader, SchemaGrader)
    return [g for g in manifest.spec.graders if isinstance(g, rules)]


async def test_a_seeded_bug_is_found_in_the_trace_and_its_fix_confirmed_by_a_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_evaluation(SUITE)
    baseline = Experiment.from_summary(json.loads(BASELINE.read_text("utf-8")))
    monkeypatch.setattr(researcher_agents, "write", write_one_source_per_site)  # the bug ships
    # Durable: on PostgreSQL when AIOS_TEST_DATABASE_URL is set.
    async with LocalRuntime.from_app(RESEARCHER, cassettes=CASSETTES, durable=True) as runtime:
        core = runtime.runtime
        handle = await runtime.chat(APP, QUESTION)
        assert (await handle.wait(timeout=60)).status == "completed"  # nothing looks wrong yet

        # The eval gate blocks it: the report cites too little.
        blocked = check_gate(await _graded(runtime, manifest), manifest.spec.gate, baseline)
        assert not blocked.passed
        assert any(reason.startswith("report-shape fell") for reason in blocked.reasons)

        # The Trace Explorer shows where: draft was given four sources and kept one, and the
        # steps after it only passed that on.
        trace = await views.trace(core, core.tenant, APP, handle.run_id)
        steps = {node.id: node for node in trace.nodes}
        given = cast(dict[str, Any], steps["draft"].input)
        kept = cast(dict[str, Any], steps["draft"].output)
        cited = {url for finding in given["findings"] for url in finding["sources"]}
        assert len(cited) == 4
        assert len(kept["sources"]) == 1
        assert steps["draft"].model_calls  # the model was asked, and the code after it dropped them
        assert steps["review"].status == "completed"

        # The fix ships, and the run is forked from the faulty step.
        monkeypatch.setattr(researcher_agents, "write", FIXED)
        await core.apply(load_app(RESEARCHER), tenant=core.tenant)  # its workers load the fix
        fork = await core.restart(APP, handle.run_id, "fork", step="draft")
        fixed = await RunHandle(runtime, APP, fork).wait(timeout=60)
        assert fixed.status == "completed", fixed.error
        assert sorted(fixed.output["sources"]) == sorted(cited)
        grades = {g.name: deterministic(g, fixed.output).passed for g in _rules(manifest)}
        assert grades == {"report-shape": True, "cites-the-spec": True, "on-topic": True}
        compared = await views.comparison(core, core.tenant, APP, handle.run_id, fork)
        copied = {row.step for row in compared.steps if row.b.copied}
        assert copied == {"plan", "research", "analyze"}  # kept, not run or paid for again

        # And the gate turns green.
        green = check_gate(await _graded(runtime, manifest), manifest.spec.gate, baseline)
        assert green.passed, green.reasons


# --------------------------------------------------------------------------- exit 6

HELPDESK = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: helpdesk, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: answer}]
  agents: [helper]
  workflows: [answer]
  policies: [desk-tools]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: helper, version: 1.0.0}
spec:
  runtime: {type: "adapter:langgraph", entrypoint: "m3_helper:build"}
  capabilities: [{name: help.answer}]
  model: {task: planning}
  tools: {allow: ["kb.article.find", "pay.refund.create"]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: answer, version: 1.0.0}
spec:
  output: "${answer.output}"
  steps:
    - {id: answer, type: task, capability: help.answer, input: {message: "${input.message}"}}
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: desk-tools, version: 1.0.0}
spec:
  subject: {app: helpdesk}
  allow:
    - {action: tool.invoke, resource: "kb.>"}
    - {action: tool.invoke, resource: "pay.>"}
  requireApproval:
    - {action: tool.invoke, resource: pay.refund.create, approvers: ["role:lead"]}
%s
"""
NO_ARTICLES = "  deny: [{action: tool.invoke, resource: kb.article.find}]"


def build(model: Any, tools: list[Any]) -> Any:
    """A ReAct loop that turns every tool error into a message to the model, as many do."""
    llm = model.bind_tools(tools)

    async def think(state: MessagesState) -> dict[str, Any]:
        return {"messages": [await llm.ainvoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("think", think)
    graph.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", tools_condition)
    graph.add_edge("tools", "think")
    return graph.compile()


@dataclass
class Scripted(Provider):
    """Asks for a refund or an article once, then answers from what the tool said."""

    name: str = "anthropic"
    requests: list[ModelRequest] = field(default_factory=list[ModelRequest])

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        self.requests.append(request)
        last = request.messages[-1]
        if last.role == "tool":
            return Completion(
                text=f"Done: {last.content}", input_tokens=20, output_tokens=6, model=entry.model
            )
        refund = str(last.content).lower().startswith("please refund")
        arguments: dict[str, JsonValue] = (
            {"order": "1001", "amountUSD": 120} if refund else {"q": "refunds"}
        )
        name = "pay__refund__create" if refund else "kb__article__find"
        call = ModelToolCall(id="toolu_1", name=name, arguments=arguments)
        return Completion(
            text="", input_tokens=10, output_tokens=4, model=entry.model, tool_calls=(call,)
        )


@dataclass
class Desk:
    runtime: LocalRuntime
    refunds: list[str] = field(default_factory=list[str])
    asked: list[str] = field(default_factory=list[str])


@pytest.fixture
async def open_desk(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Callable[..., Any]]:
    module = ModuleType("m3_helper")
    module.build = build  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "m3_helper", module)
    running: list[LocalRuntime] = []

    async def make(policy: str = "") -> Desk:
        runtime = LocalRuntime(tenant="acme", providers={"anthropic": Scripted()})
        await runtime.__aenter__()
        running.append(runtime)
        desk = Desk(runtime)

        @tool("kb.article.find", side_effect=SideEffect.READ, description="Find help articles.")
        async def find(q: str) -> dict[str, Any]:
            desk.asked.append(q)
            return {"days": 30}

        @tool("pay.refund.create", side_effect=SideEffect.FINANCIAL, description="Refund an order.")
        async def refund(order: str, amountUSD: float) -> dict[str, Any]:
            desk.refunds.append(order)
            return {"refunded": amountUSD}

        await runtime.runtime.register_tool_provider(NativeProvider("kb", [find]))
        await runtime.runtime.register_tool_provider(NativeProvider("pay", [refund]))
        await runtime.runtime.apply(load_manifests(HELPDESK % policy), tenant="acme")
        return desk

    yield make
    for runtime in running:
        await runtime.__aexit__(None, None, None)


def _ctx(span: ReadableSpan) -> SpanContext:
    assert span.context is not None
    return span.context


def _data(event: Any) -> dict[str, JsonValue]:
    return event.data if isinstance(event.data, dict) else {}


async def test_a_langgraph_agent_is_traced_and_waits_for_a_person(open_desk: Any) -> None:
    desk: Desk = await open_desk()
    handle = await desk.runtime.chat("helpdesk", "Please refund order 1001")
    asked = ApprovalRequested.model_validate(
        (await handle.wait_for_event("approval.requested", timeout=20)).data
    )
    assert asked.approvers == ["role:lead"]
    assert desk.refunds == []  # the graph catches tool errors, but a pause isn't one

    await desk.runtime.approve(asked.approval, by="user:lead@acme")
    result = await handle.wait(timeout=20)
    assert result.status == "completed", result.error
    assert desk.refunds == ["1001"]  # once
    assert result.output["answer"].startswith("Done:")

    spans = spans_for(result.events, bus=None, exporter=InMemorySpanExporter())  # type: ignore[arg-type]
    by_id = {_ctx(span).span_id: span for span in spans}
    assert {_ctx(span).trace_id for span in spans} == {_ctx(spans[0]).trace_id}
    for span in spans:  # nothing hangs loose: each span sits under one of the run's own
        if span.name != "run answer":
            assert span.parent is not None, span.name
            assert span.parent.span_id in by_id, span.name
    inner = [span for span in spans if span.parent and span.name != "run answer"]
    parent = {span.name: by_id[span.parent.span_id].name for span in inner if span.parent}
    assert parent["step answer"] == "run answer"
    assert parent["invoke_agent helper"] == "step answer"
    for span in spans:
        if span.name.startswith(("chat ", "execute_tool ")):
            assert by_id[span.parent.span_id].name == "invoke_agent helper"  # type: ignore[union-attr]
    helped = sorted(
        (span for span in spans if span.name == "invoke_agent helper"),
        key=lambda span: span.end_time or 0,
    )
    assert [bool((s.attributes or {}).get(SUSPENDED)) for s in helped] == [True, False]
    refunded = sorted(
        (span for span in spans if span.name == "execute_tool pay.refund.create"),
        key=lambda span: span.end_time or 0,
    )
    assert [bool((s.attributes or {}).get(SUSPENDED)) for s in refunded] == [True, False]
    decisions = [e.type for e in result.events if e.type.startswith("policy.decision.")]
    assert "policy.decision.escalated" in decisions


async def test_a_langgraph_agent_cant_use_a_tool_policy_denies(open_desk: Any) -> None:
    desk: Desk = await open_desk(NO_ARTICLES)
    result = await (await desk.runtime.chat("helpdesk", "How long do returns take?")).wait(
        timeout=20
    )
    assert desk.asked == []  # the tool never ran
    denied = [_data(e) for e in result.events if e.type == "policy.decision.denied"]
    assert [d.get("resource") for d in denied] == ["kb.article.find"]
    assert not [e for e in result.events if e.type == "tool.call.completed"]

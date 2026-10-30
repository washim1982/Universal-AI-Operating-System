"""Observability, API and CLI on a real recorded AI Researcher run (WP 1.6, 1.7)."""

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx2
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext

from aios.api import create_app
from aios.cli import client as cli_client
from aios.cli.main import main
from aios.cli.scaffold import init
from aios.controlplane import load_app
from aios.core.envelope import Event
from aios.core.manifests import AppManifest, load_manifests
from aios.observability import EventMetrics, JsonFormatter, redact, render, spans_for
from aios.observability import semconv as sc
from aios.testing.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps" / "ai-researcher"
CASSETTES = ROOT / "tests" / "cassettes" / "ai-researcher"
QUESTION = "What changed in the OpenTelemetry GenAI semantic conventions in 2026?"


@pytest.fixture(scope="module")
def run_events() -> list[Event]:
    async def record() -> list[Event]:
        async with LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime:
            result = await (await runtime.chat("ai-researcher", QUESTION)).wait(timeout=60)
        assert result.status == "completed"
        return result.events

    return asyncio.run(record())


# --------------------------------------------------------------------------- observability


def _parent(span: ReadableSpan) -> int:
    assert span.parent is not None
    return span.parent.span_id


def _ctx(span: ReadableSpan) -> SpanContext:
    assert span.context is not None
    return span.context


def _attrs(span: ReadableSpan) -> Mapping[str, Any]:
    assert span.attributes is not None
    return span.attributes


def test_traces_nest_runs_steps_agents_and_model_calls(run_events: list[Event]) -> None:
    spans = spans_for(run_events, bus=None, exporter=InMemorySpanExporter())  # type: ignore[arg-type]
    by_name = {span.name: span for span in spans}
    run = by_name["run research-report"]
    plan = by_name["step plan"]
    planner = by_name["invoke_agent planner-agent"]
    model = next(s for s in spans if s.name.startswith("chat ") and _attrs(s)[sc.STEP] == "plan")
    assert {_ctx(s).trace_id for s in spans} == {_ctx(run).trace_id}
    assert _parent(plan) == _ctx(run).span_id
    assert _parent(planner) == _ctx(plan).span_id
    assert _parent(model) == _ctx(planner).span_id
    assert _attrs(model)[sc.PROVIDER] == "anthropic"
    assert _attrs(model)[sc.INPUT_TOKENS] == 912
    tools = [s for s in spans if s.name == "execute_tool search.web.query"]
    assert len(tools) == 3
    assert len([s for s in spans if s.name == "invoke_agent research-agent"]) == 3
    assert run.end_time is not None
    assert run.start_time is not None
    assert run.end_time >= run.start_time


def test_the_timeline_renders_the_causal_tree(run_events: list[Event]) -> None:
    text = render(run_events)
    lines = text.splitlines()
    assert lines[0].split()[1] == "app.request.received"
    assert "└─ run.started" in lines[1]
    assert any("model.call.completed" in line and "anthropic/claude" in line for line in lines)
    assert any("run.completed" in line for line in lines)
    plain = render(run_events, ascii=True)
    assert "`- run.started" in plain.splitlines()[1]
    plain.encode("ascii")


def test_metrics_count_runs_tokens_and_cost(run_events: list[Event]) -> None:
    metrics = EventMetrics(bus=None)  # type: ignore[arg-type]
    for event in run_events:
        metrics.observe(event)
    text = metrics.exposition().decode()
    assert 'aios_runs_total{app="ai-researcher",outcome="completed"' in text
    assert "aios_model_cost_usd_total" in text
    assert 'aios_tool_calls_total{app="ai-researcher",outcome="completed"' in text


def test_logs_are_json_and_secrets_are_redacted() -> None:
    record = logging.LogRecord(
        "aios.kernel", logging.INFO, __file__, 1, "key sk-ant-abcdefghijklmnop", None, None
    )
    record.run_id = "01RUN"
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "key [REDACTED]"
    assert entry["run_id"] == "01RUN"
    assert redact('{"api_key": "XXXXXX"}') == '{"[REDACTED]"}'
    assert redact("Authorization: Bearer abcdefghijklmnop") == "Authorization: [REDACTED]"


# --------------------------------------------------------------------------- API


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx2.AsyncClient, LocalRuntime]]:
    async with LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime:
        metrics = EventMetrics(runtime.bus)
        await metrics.start()
        app = create_app(runtime.runtime, metrics=metrics)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as client:
            yield client, runtime
        await metrics.stop()


async def _until_done(client: httpx2.AsyncClient, run: str) -> dict[str, Any]:
    async with asyncio.timeout(30):
        while True:
            view = (await client.get(f"/v1/apps/ai-researcher/runs/{run}")).json()
            if view["status"] != "running":
                return view
            await asyncio.sleep(0.05)


async def test_the_api_runs_an_app_end_to_end(api: tuple[httpx2.AsyncClient, LocalRuntime]) -> None:
    client, _ = api
    assert (await client.get("/healthz")).json()["status"] == "ok"
    assert [a["app"] for a in (await client.get("/v1/apps")).json()] == ["ai-researcher"]
    started = await client.post(
        "/v1/apps/ai-researcher/runs",
        json={"message": QUESTION},
        headers={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
    )
    assert started.status_code == 202
    run = started.json()["run_id"]
    view = await _until_done(client, run)
    assert view["status"] == "completed"
    assert view["output"]["sources"]
    assert view["usage"]["cost_usd"] > 0
    events = (await client.get(f"/v1/apps/ai-researcher/runs/{run}/events")).json()
    assert events[0]["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert {e["traceparent"].split("-")[1] for e in events} == {"4bf92f3577b34da6a3ce929d0e0e4736"}
    streamed: list[str] = []
    async with client.stream(
        "GET", f"/v1/apps/ai-researcher/runs/{run}/events", headers={"Accept": "text/event-stream"}
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                streamed.append(line[7:])
    assert streamed[-1] == "run.completed"
    assert len(streamed) == len(events)
    log = (
        await client.get("/v1/events", params={"app": "ai-researcher", "pattern": "run.*"})
    ).json()
    assert [e["type"] for e in log] == ["run.started", "run.completed"]
    metrics = (await client.get("/metrics")).text
    assert "aios_runs_total" in metrics
    assert (await client.get("/v1/dlq")).json() == []


async def test_the_api_rejects_unknown_apps_and_bad_manifests(
    api: tuple[httpx2.AsyncClient, LocalRuntime],
) -> None:
    client, _ = api
    assert (await client.post("/v1/apps/nope/runs", json={"message": "x"})).status_code == 404
    bad = await client.post(
        "/v1/apply", content="kind: App\n", headers={"Content-Type": "application/yaml"}
    )
    assert bad.status_code == 422
    assert (await client.post("/v1/dlq/01ABC/redrive")).status_code == 404


async def test_apply_installs_an_app_into_a_running_runtime(
    api: tuple[httpx2.AsyncClient, LocalRuntime], tmp_path: Path
) -> None:
    client, runtime = api
    root = init("hello-app", tmp_path)
    document = (root / "manifests" / "app.yaml").read_text(encoding="utf-8")
    sys.path.insert(0, str(root / "src"))
    try:
        applied = await client.post(
            "/v1/apply", content=document, headers={"Content-Type": "application/yaml"}
        )
        assert applied.status_code == 201, applied.text
        assert applied.json() == {
            "app": "hello-app",
            "version": "0.1.0",
            "agents": ["assistant-agent"],
            "workflows": ["answer"],
        }
        assert len(runtime.runtime.workers) == 2
    finally:
        sys.path.remove(str(root / "src"))


# --------------------------------------------------------------------------- CLI


def test_init_scaffolds_a_valid_app(tmp_path: Path) -> None:
    root = init("my-app", tmp_path)
    manifests = load_manifests(root / "manifests" / "app.yaml")
    assert {type(m).__name__ for m in manifests} == {
        "AppManifest",
        "AgentManifest",
        "WorkflowManifest",
    }
    [app] = [m for m in manifests if isinstance(m, AppManifest)]
    assert app.metadata.name == "my-app"
    assert (root / "src" / "my_app" / "agents.py").exists()
    with pytest.raises(SystemExit):
        init("my-app", tmp_path)
    with pytest.raises(SystemExit):
        init("Not Valid", tmp_path)


def test_server_roles_need_a_database_except_dev() -> None:
    with pytest.raises(SystemExit, match="database-url"):
        main(["worker"])


def test_cli_client_commands_print_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], run_events: list[Event]
) -> None:
    events = [e.model_dump(mode="json", exclude_none=True) for e in run_events]
    last: dict[str, Any] = run_events[-1].data  # type: ignore[assignment]

    def handle(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            return httpx2.Response(202, json={"run_id": "R1"})
        if path.endswith("/events") and request.headers.get("accept") == "text/event-stream":
            body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
            return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})
        if path.endswith("/events"):
            return httpx2.Response(200, json=events)
        return httpx2.Response(
            200, json={"status": "completed", "output": last["output"], "usage": last["usage"]}
        )

    original = httpx2.Client

    def fake(*args: Any, **kwargs: Any) -> httpx2.Client:
        return original(*args, transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", fake)
    assert main(["run", "ai-researcher", QUESTION]) == 0
    out = capsys.readouterr().out
    assert "run R1" in out
    assert "status=completed" in out
    assert main(["trace", "R1", "--app", "ai-researcher"]) == 0
    assert "run.started" in capsys.readouterr().out
    assert main(["logs", "R1", "--app", "ai-researcher"]) == 0
    assert "task.requested" in capsys.readouterr().out


def test_app_configuration_is_validated_against_its_schema() -> None:
    desk = ROOT / "apps" / "ai-support-desk"
    with pytest.raises(ValueError, match="invalid configuration"):
        load_app(desk, config={"refundApprovalThresholdUSD": -5})
    with pytest.raises(FileNotFoundError, match="not an App directory"):
        load_app(ROOT / "docs")

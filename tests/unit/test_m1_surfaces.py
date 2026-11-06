"""Observability, API and CLI on a real recorded AI Researcher run (WP 1.6, 1.7)."""

import asyncio
import io
import json
import logging
import re
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
from aios.cli.server import demo_page, running
from aios.controlplane import load_app
from aios.core.catalog import STANDARD_EVENTS
from aios.core.envelope import Event
from aios.core.manifests import AppManifest, load_manifests
from aios.core.policy import SideEffect
from aios.observability import EventMetrics, JsonFormatter, redact, render, spans_for
from aios.observability import semconv as sc
from aios.testing.runtime import LocalRuntime
from aios.tools import NativeProvider, tool

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


def test_embedding_calls_get_their_own_span_name(run_events: list[Event]) -> None:
    call = next(e for e in run_events if e.type == "model.call.completed")
    assert isinstance(call.data, dict)
    embedded = call.model_copy(update={"data": {**call.data, "operation": "embed"}})
    spans = spans_for([embedded], bus=None, exporter=InMemorySpanExporter())  # type: ignore[arg-type]
    assert spans[0].name.startswith("embeddings ")
    assert _attrs(spans[0])[sc.OPERATION] == sc.OP_EMBEDDINGS


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


async def _view(client: httpx2.AsyncClient, run: str, status: str) -> dict[str, Any]:
    async with asyncio.timeout(10):
        while True:
            view = (await client.get(f"/v1/apps/waiter/runs/{run}")).json()
            if view["status"] == status:
                return view
            await asyncio.sleep(0.02)


async def test_runs_can_be_paused_resumed_and_signalled_through_the_api(
    api: tuple[httpx2.AsyncClient, LocalRuntime],
) -> None:
    client, runtime = api
    await runtime.runtime.apply(load_manifests(WAITER))
    base = "/v1/apps/waiter/runs"
    run = (await client.post(base, json={"message": "hi"})).json()["run_id"]
    view = await _view(client, run, "paused")
    assert view["waiting"] == ["signal:reply"]
    assert view["steps"] == {"reply": "running"}

    conflict = await client.post(f"{base}/{run}/resume")
    assert conflict.status_code == 409  # waiting for a signal, which resume doesn't answer
    assert "not paused by request" in conflict.json()["detail"]
    assert (await client.post(f"{base}/{run}/pause")).status_code == 202
    async with asyncio.timeout(10):
        while True:
            if "request" in (await _view(client, run, "paused"))["waiting"]:
                break
            await asyncio.sleep(0.02)
    assert (await client.post(f"{base}/{run}/signal", json={"name": "Bad Name"})).status_code == 422
    sent = await client.post(f"{base}/{run}/signal", json={"name": "reply", "data": {"ok": True}})
    assert sent.status_code == 202
    assert (await client.post(f"{base}/{run}/resume")).status_code == 202

    view = await _view(client, run, "completed")
    assert view["output"] == {"ok": True}
    assert view["waiting"] == []
    assert (await client.post(f"{base}/{run}/pause")).status_code == 409
    assert (await client.post(f"{base}/{run}/resume")).status_code == 409
    assert (await client.post(f"{base}/{run}/signal", json={"name": "reply"})).status_code == 409
    assert (await client.post(f"{base}/no-such-run/pause")).status_code == 404


BILLING = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: billing, version: 0.1.0}
spec:
  entrypoints: [{type: chat, workflow: refund}]
  workflows: [refund]
  config: {secrets: [BILLING_KEY]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: refund, version: 0.1.0}
spec:
  steps:
    - {id: pay, type: tool, tool: billing.refund.create, input: {amount: 5}}
"""


async def test_a_workflow_tool_gets_its_credential_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "XXXXXX"
    monkeypatch.setenv("AIOS_SECRET_BILLING_KEY", secret)
    used: list[str] = []

    @tool("billing.refund.create", side_effect=SideEffect.WRITE, credentials=["BILLING_KEY"])
    async def refund(amount: int, credentials: Mapping[str, str]) -> dict[str, Any]:
        used.append(credentials["BILLING_KEY"])
        return {"refunded": amount, "sent": f"Bearer {credentials['BILLING_KEY']}"}

    async with LocalRuntime(tenant="local") as runtime:
        await runtime.runtime.apply(load_manifests(BILLING))
        await runtime.runtime.register_tool_provider(NativeProvider("billing", [refund]))
        result = await (await runtime.chat("billing", "refund")).wait(timeout=10)

    assert used == [secret]
    assert result.status == "completed"
    assert result.output == {"refunded": 5, "sent": "Bearer [REDACTED]"}
    assert secret not in json.dumps([e.model_dump(mode="json") for e in result.events])


def _names_in(directory: Path) -> list[str]:
    return [path.name for path in directory.iterdir()]


async def test_a_local_runtime_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMP", str(tmp_path))
    async with LocalRuntime(tenant="local") as runtime:
        (runtime.sandbox / "note.txt").write_text("from a tool", "utf-8")
    assert _names_in(tmp_path) == []


async def test_a_run_that_has_not_started_cannot_be_paused(
    api: tuple[httpx2.AsyncClient, LocalRuntime],
) -> None:
    client, runtime = api
    await runtime.kill_worker()  # the engine, which starts runs, is down
    base = "/v1/apps/ai-researcher/runs"
    run = (await client.post(base, json={"message": "hi"})).json()["run_id"]
    assert (await client.get(f"{base}/{run}")).json()["status"] == "running"
    refused = await client.post(f"{base}/{run}/pause")
    assert refused.status_code == 409
    assert "has not started" in refused.json()["detail"]
    assert (await client.post(f"{base}/{run}/signal", json={"name": "reply"})).status_code == 202


async def test_apply_installs_an_app_into_a_running_runtime(
    api: tuple[httpx2.AsyncClient, LocalRuntime], tmp_path: Path
) -> None:
    client, runtime = api
    root = init("hello-app", tmp_path)
    document = (root / "manifests" / "app.yaml").read_text(encoding="utf-8")
    sys.path.insert(0, str(root / "src"))
    try:
        applied = await client.post(
            "/v1/apply",
            params={"consent": "all"},
            content=document,
            headers={"Content-Type": "application/yaml"},
        )
        assert applied.status_code == 201, applied.text
        found = applied.json()
        assert (found["app"], found["version"]) == ("hello-app", "0.1.0")
        assert found["status"] == "active"
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


def test_only_the_dev_server_offers_an_apps_demo_page(tmp_path: Path) -> None:
    assert demo_page("dev", [tmp_path, APP]) == APP / "demo.html"
    assert demo_page("dev", [tmp_path]) is None
    assert demo_page("api", [APP]) is None


async def test_the_api_serves_a_demo_page_and_says_where_answers_come_from() -> None:
    async with LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime:
        app = create_app(runtime.runtime, demo=APP / "demo.html")
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as client:
            page = await client.get("/demo")
            health = (await client.get("/healthz")).json()
        without = httpx2.ASGITransport(app=create_app(runtime.runtime))
        async with httpx2.AsyncClient(transport=without, base_url="http://aios") as client:
            assert (await client.get("/demo")).status_code == 404
    assert page.headers["content-type"].startswith("text/html")
    assert "AI Researcher" in page.text
    assert health["answers"] == "recorded"
    assert "/demo" not in app.openapi()["paths"]  # not part of the versioned API


def test_the_demo_page_handles_real_events_and_never_builds_html_from_text() -> None:
    page = (APP / "demo.html").read_text(encoding="utf-8")
    handled = re.findall(r'^  "([a-z.]+)"\(run, event\)', page, re.MULTILINE)
    assert len(handled) >= 10
    assert set(handled) - {"research.findings.recorded"} <= {s.type for s in STANDARD_EVENTS}
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", page)


async def test_a_configured_private_model_serves_every_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIOS_PRIVATE_MODEL", "llama-4")
    monkeypatch.setenv("AIOS_PRIVATE_MODEL_URL", "http://llama.local/v1")
    options = {
        "tenants": ["local"],
        "database_url": None,
        "cassettes": None,
        "cassette_mode": "live",
    }
    async with running("api", apps=[APP], **options) as (runtime, _):  # type: ignore[arg-type]
        policy = runtime.models.policy_for("local", "ai-researcher")
        assert policy.spec.default.candidates == ["private/llama-4"]
        assert policy.spec.rules == []
    monkeypatch.delenv("AIOS_PRIVATE_MODEL_URL")
    with pytest.raises(SystemExit, match="AIOS_PRIVATE_MODEL_URL"):
        async with running("api", apps=[APP], **options):  # type: ignore[arg-type]
            pass


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


@pytest.mark.parametrize(
    ("argv", "path", "body"),
    [
        (["pause", "R1", "--app", "a"], "/v1/apps/a/runs/R1/pause", None),
        (["resume", "R1", "--app", "a"], "/v1/apps/a/runs/R1/resume", None),
        (["cancel", "R1", "--app", "a"], "/v1/apps/a/runs/R1/cancel", None),
        (["signal", "R1", "reply", "--app", "a"], "/v1/apps/a/runs/R1/signal", (None,)),
        (
            ["signal", "R1", "reply", "--app", "a", "--data", '{"ok": true}'],
            "/v1/apps/a/runs/R1/signal",
            ({"ok": True},),
        ),
        (
            ["signal", "R1", "reply", "--app", "a", "--data", "plain words"],
            "/v1/apps/a/runs/R1/signal",
            ("plain words",),
        ),
    ],
)
def test_cli_controls_a_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    path: str,
    body: tuple[Any] | None,
) -> None:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(202, json={"status": "accepted"})

    original = httpx2.Client

    def fake(*args: Any, **kwargs: Any) -> httpx2.Client:
        return original(*args, transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", fake)
    assert main(argv) == 0
    assert "accepted" in capsys.readouterr().out
    [sent] = requests
    assert (sent.method, sent.url.path) == ("POST", path)
    if body is not None:
        assert json.loads(sent.content) == {"name": "reply", "data": body[0]}


def test_cli_shows_a_runs_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/apps/a/runs/R1"
        return httpx2.Response(200, json={"status": "paused", "waiting": ["request"]})

    original = httpx2.Client

    def fake(*args: Any, **kwargs: Any) -> httpx2.Client:
        return original(*args, transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", fake)
    assert main(["status", "R1", "--app", "a"]) == 0
    assert json.loads(capsys.readouterr().out)["waiting"] == ["request"]


def test_cli_run_says_why_a_run_failed_on_any_console() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(202, json={"run_id": "R1"})
        if request.url.path.endswith("/events"):
            event = {"type": "run.failed", "time": "2026-10-30T12:00:00.000Z"}
            return httpx2.Response(
                200,
                text=f"data: {json.dumps(event)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(
            200,
            json={
                "status": "failed",
                "output": {"title": "a → b"},  # real models write what cp1252 can't encode
                "error": {"code": "model.failed", "message": "anthropic 401: invalid key"},
            },
        )

    client = cli_client.Client("http://aios")
    client.http = httpx2.Client(base_url="http://aios", transport=httpx2.MockTransport(handle))
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252")
    assert cli_client.run_and_follow(client, "ai-researcher", "q", console) == 1
    console.flush()
    printed = raw.getvalue().decode("cp1252")
    assert "error: anthropic 401: invalid key" in printed
    assert "a \\u2192 b" in printed


def test_cli_says_when_another_server_answered() -> None:
    llama = httpx2.MockTransport(lambda _: httpx2.Response(401, json={"error": {"code": 401}}))
    client = cli_client.Client("http://127.0.0.1:8080/")
    client.http = httpx2.Client(base_url=client.server, transport=llama)
    with pytest.raises(SystemExit, match=r"error 401: http://127\.0\.0\.1:8080 did not answer"):
        client.apps()


def test_cli_keeps_the_detail_of_an_aios_error() -> None:
    aios = httpx2.MockTransport(lambda _: httpx2.Response(404, json={"detail": "no such App"}))
    client = cli_client.Client("http://aios")
    client.http = httpx2.Client(base_url=client.server, transport=aios)
    with pytest.raises(SystemExit, match="error 404: no such App"):
        client.apps()


def test_app_configuration_is_validated_against_its_schema() -> None:
    desk = ROOT / "apps" / "ai-support-desk"
    with pytest.raises(ValueError, match="invalid configuration"):
        load_app(desk, config={"refundApprovalThresholdUSD": -5})
    with pytest.raises(FileNotFoundError, match="not an App directory"):
        load_app(ROOT / "docs")

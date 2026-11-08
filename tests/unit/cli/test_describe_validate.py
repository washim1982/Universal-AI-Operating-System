"""``aios describe`` and ``aios validate`` (FR-CP-03; WP 3.6)."""

from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.api import create_app
from aios.cli.describe import (
    agent_text,
    app_text,
    approval_text,
    describe,
    run_text,
    task_text,
)
from aios.cli.main import main
from aios.cli.scaffold import init
from aios.cli.validate import validate
from aios.controlplane import load_app
from aios.testing.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[3]
PEOPLE_DESK = ROOT / "tests" / "fixtures" / "apps" / "people-desk"


# ---------------------------------------------------------------------- describe


async def test_the_api_says_what_an_app_is_made_of() -> None:
    async with LocalRuntime(tenant="acme") as runtime:
        runtime.runtime.install(load_app(PEOPLE_DESK), tenant="acme")
        transport = httpx2.ASGITransport(app=create_app(runtime.runtime))
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
            found = (await http.get("/v1/apps/people-desk")).json()
            assert (await http.get("/v1/apps/nothing")).status_code == 404
    assert (found["app"], found["version"]) == ("people-desk", "0.1.0")
    [agent] = found["agents"]
    assert (agent["agent"], agent["runtime"], agent["capabilities"]) == (
        "refund-agent",
        "python",
        ["refunds.issue"],
    )
    assert found["humans"] == [{"capability": "refunds.review", "assignees": ["role:support-lead"]}]
    [workflow] = found["workflows"]
    assert [(s["id"], s["capability"], s["needs"]) for s in workflow["steps"]] == [
        ("review", "refunds.review", []),
        ("issue", "refunds.issue", ["review"]),
    ]
    assert found["policies"] == ["refunds"]
    text = app_text(found, [{"run_id": "01RUN", "status": "completed", "started_at": "today"}])
    assert "refund-agent (python): provides refunds.issue; may use filesystem.file.write" in text
    assert "refunds.review: role:support-lead" in text
    assert "    issue: task refunds.issue after review" in text
    assert "01RUN  completed" in text


TRACE: dict[str, Any] = {
    "workflow": "refund",
    "version": "0.1.0",
    "started_at": "2026-11-06T10:00:00Z",
    "duration_ms": 2500,
    "input_tokens": 120,
    "output_tokens": 30,
    "cost_usd": 0.0021,
    "nodes": [
        {
            "id": "review",
            "type": "task",
            "status": "completed",
            "capability": "refunds.review",
            "duration_ms": 900,
            "model_calls": [],
            "tool_calls": [],
            "approvals": [],
        },
        {
            "id": "issue",
            "type": "task",
            "status": "failed",
            "capability": "refunds.issue",
            "duration_ms": 40,
            "model_calls": [{}],
            "tool_calls": [{}],
            "error": "policy.denied: as changed, it needs another approval",
            "approvals": [{"approval": "01APP", "status": "granted", "by": "user:sam@acme"}],
        },
    ],
}


def test_a_run_in_words() -> None:
    run: dict[str, Any] = {
        "run_id": "01RUN",
        "app": "people-desk",
        "status": "failed",
        "waiting": [],
        "tainted": True,
        "usage": {"cost_usd": 0.0021},
        "error": {"message": "step issue failed"},
        "output": None,
        "source": {"mode": "fork", "run": "01OLD", "step": "issue"},
    }
    text = run_text(run, TRACE)
    assert "Status:         failed" in text
    assert "Took:           2.5s" in text
    assert "Cost:           $0.0021, 120 tokens in, 30 out" in text
    assert "Tainted:        yes" in text
    assert "From:           fork of 01OLD from step issue" in text
    assert "  issue          failed     refunds.issue  (1 model, 1 tool calls, 40ms)" in text
    assert "    error: policy.denied: as changed, it needs another approval" in text
    assert "    approval 01APP: granted by user:sam@acme" in text
    assert "Error:          step issue failed" in text


def test_an_approval_a_task_and_an_agent_in_words() -> None:
    approval = approval_text(
        {
            "approval": "01APP",
            "status": "pending",
            "summary": "orders.refund.create(order=1001)",
            "risk": "high",
            "app": "shop",
            "run": "01RUN",
            "initiator": "user:ann@acme",
            "approvers": ["group:finance"],
            "quorum": 2,
            "votes": [{"by": "user:bob@acme", "comment": None}],
            "policies": ["refunds/approval/0"],
            "expires_at": "tomorrow",
            "preview": "would refund $120.00 for order 1001",
            "payload": {"order": "1001", "amountUSD": 120},
            "rationale": "It arrived broken.",
            "tainted": False,
        }
    )
    assert "Approvals:      1 of 2" in approval
    assert "It would:\n  would refund $120.00 for order 1001" in approval
    assert '"amountUSD": 120' in approval
    assert "The agent says (a claim, not a fact):\n  It arrived broken." in approval
    task = task_text(
        {
            "task": "01TSK",
            "status": "completed",
            "capability": "legal.review",
            "description": "Read it.",
            "app": "desk",
            "run": "01RUN",
            "assignees": ["group:legal"],
            "by": "user:ann@acme",
            "input": {"contract": "nda-7"},
            "output_schema": None,
            "output": {"verdict": "sign"},
        }
    )
    assert "What to do:     Read it." in task
    assert 'Answer:\n  {\n    "verdict": "sign"\n  }' in task
    agent = agent_text(
        {
            "app": "desk",
            "agent": "helper",
            "version": "1.0.0",
            "capabilities": ["help.answer"],
            "tools": [],
            "memory_read": ["app"],
            "memory_write": [],
            "suspended": True,
            "suspension": "looping",
            "completed": 3,
            "failed": 1,
        }
    )
    assert "May use:        no tools" in agent
    assert "State:          stopped: looping" in agent
    assert "Recent tasks:   3 completed, 1 failed" in agent


class Api:
    def app(self, app: str) -> Any:
        return {"app": app, "version": "1.0.0", "agents": [], "workflows": []}

    def runs(self, app: str, limit: int) -> Any:
        return []

    def run(self, app: str, run_id: str) -> Any:
        return {"run_id": run_id, "app": app, "status": "running"}

    def trace_of(self, app: str, run_id: str) -> Any:
        return {"nodes": []}

    def agents(self) -> Any:
        return [
            {"app": "a", "agent": "helper", "version": "1", "capabilities": []},
            {"app": "b", "agent": "helper", "version": "2", "capabilities": []},
        ]

    def approval(self, approval: str) -> Any:
        raise AssertionError

    def task(self, task: str) -> Any:
        raise AssertionError


def test_describe_asks_for_what_it_needs() -> None:
    assert "Recent runs:    none yet" in describe(Api(), "app", "shop")
    assert "Status:         running" in describe(Api(), "run", "01RUN", app="shop")
    assert describe(Api(), "agent", "helper").count("Agent:") == 2  # one per App
    assert "helper 2" in describe(Api(), "agent", "helper", app="b")
    with pytest.raises(SystemExit, match="--app"):
        describe(Api(), "run", "01RUN")
    with pytest.raises(SystemExit, match="no agent nobody"):
        describe(Api(), "agent", "nobody")


# ---------------------------------------------------------------------- validate


def test_a_template_app_is_valid(tmp_path: Path) -> None:
    report = validate(init("shop-one", tmp_path, "approval"))
    assert report.ok, report.text()
    assert report.warnings == []
    assert report.summary == (
        "shop-one 0.1.0: 1 agent(s), 0 done by people, 1 workflow(s), 1 policy(ies)"
    )


def test_what_only_shows_up_later_is_found_now(tmp_path: Path) -> None:
    root = init("shop-two", tmp_path, "chat")
    app_yaml = root / "manifests" / "app.yaml"
    text = app_yaml.read_text("utf-8")
    text = text.replace("capability: answering, input", "capability: answer.it, input")
    text = text.replace(
        "  limits: {maxSteps: 5",
        '  tools: {allow: ["orders.refund.create"]}\n  limits: {maxSteps: 5',
    )
    app_yaml.write_text(text, encoding="utf-8")
    report = validate(root)
    assert not report.ok
    assert report.errors == [
        "workflow answer, step answer: nothing provides 'answer.it' "
        "(no agent's capability, nothing in spec.humans)"
    ]
    assert "agent assistant-agent: no workflow step asks for what it provides" in report.warnings
    assert any("orders.refund.create is on its list, but no policy" in w for w in report.warnings)


def test_an_app_that_wont_load_is_not_valid(tmp_path: Path) -> None:
    root = init("shop-three", tmp_path, "chat")
    (root / "src" / "shop_three" / "agents.py").write_text("import nothing_like_this\n", "utf-8")
    report = validate(root)
    assert not report.ok
    assert "nothing_like_this" in report.errors[0]
    broken = init("shop-four", tmp_path, "chat")
    (broken / "manifests" / "app.yaml").write_text("kind: App\nspec: [", "utf-8")
    assert not validate(broken).ok
    assert not validate(tmp_path / "missing").ok


def test_the_command_says_valid_or_not(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = init("shop-five", tmp_path, "rag")
    assert main(["validate", str(good)]) == 0
    assert capsys.readouterr().out.strip().endswith("1 workflow(s), 0 policy(ies)")
    assert main(["validate", str(tmp_path / "missing")]) == 1
    assert "not valid" in capsys.readouterr().out

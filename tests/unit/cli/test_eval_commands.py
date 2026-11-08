"""``aios eval``: a suite run here or by the server, and the gate as the exit code (M3 exit 3)."""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.cli import client as cli_client
from aios.cli.main import main

ROOT = Path(__file__).resolve().parents[3]
RESEARCHER = ROOT / "apps" / "ai-researcher"
SUITE = RESEARCHER / "evals" / "quality.yaml"
BASELINE = RESEARCHER / "evals" / "baseline.json"
CASSETTES = ROOT / "tests" / "cassettes" / "ai-researcher"
LOCAL = ["--app-dir", str(RESEARCHER), "--cassettes", str(CASSETTES)]


def test_ci_runs_the_researchers_gate_here(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "summary.json"
    code = main(["eval", "run", str(SUITE), *LOCAL, "--baseline", str(BASELINE), "--out", str(out)])
    said = capsys.readouterr().out
    assert code == 0, said
    assert "gate: passed" in said
    assert "otel-genai-2026" in said
    assert json.loads(out.read_text("utf-8"))["status"] == "completed"


def test_a_baseline_it_falls_below_fails_ci(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    better = json.loads(BASELINE.read_text("utf-8"))
    for case in better["cases"]:
        for grade in case["grades"]:
            grade["score"] = 1.0
    stricter = tmp_path / "baseline.json"
    stricter.write_text(json.dumps(better), "utf-8")
    code = main(["eval", "run", str(SUITE), *LOCAL, "--baseline", str(stricter)])
    said = capsys.readouterr().out
    assert code == 1
    assert "gate: FAILED" in said
    assert "below the baseline" in said


class Server:
    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        if request.method == "POST" and request.url.path.endswith("/experiments"):
            return httpx2.Response(202, json={"experiment": "01EXP", "app": "ai-researcher"})
        if request.method == "POST":
            return httpx2.Response(200, json=_summary(1.0))
        return httpx2.Response(200, json=_summary(0.5))


def _summary(score: float) -> dict[str, Any]:
    """What the server answers for an experiment."""
    grade: dict[str, Any] = {"grader": "on-topic", "score": score, "passed": score >= 0.5}
    case: dict[str, Any] = {"case": "one", "status": "completed", "grades": [grade], "score": score}
    found: dict[str, Any] = {
        "experiment": "01EXP",
        "app": "ai-researcher",
        "suite": "research-quality",
        "suite_version": "0.1.0",
        "status": "completed",
        "cases": [case],
        "score": score,
        "passed": 1,
        "failed": 0,
        "pending": 0,
        "drifted": 0,
        "cost_usd": 0.0,
        "metrics": {"on-topic": score},
        "gate_result": {"passed": True, "reasons": []},
    }
    return found


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    found = Server()
    original = httpx2.Client

    def client(**kwargs: Any) -> httpx2.Client:
        return original(transport=httpx2.MockTransport(found), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", client)
    return found


def test_the_server_runs_it_and_the_gate_still_decides(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["eval", "run", str(SUITE), "--label", "shorter prompt"])
    asked = json.loads(server.seen[0].content)
    assert server.seen[0].url.path == "/v1/apps/ai-researcher/experiments"
    assert asked["suite"]["metadata"]["name"] == "research-quality"
    assert asked["label"] == "shorter prompt"
    assert code == 1  # 0.5 is below the suite's minimum
    assert "below the minimum" in capsys.readouterr().out


def test_a_grade_is_sent_as_given(server: Server) -> None:
    arguments = ["eval", "grade", "01EXP", "--app", "ai-researcher", "--case", "one"]
    assert main([*arguments, "--grader", "tone", "--score", "0.5", "--comment", "warm"]) == 0
    assert json.loads(server.seen[0].content) == {
        "case": "one",
        "grader": "tone",
        "score": 0.5,
        "comment": "warm",
    }


def test_a_grade_needs_its_case_grader_and_score(server: Server) -> None:
    with pytest.raises(SystemExit, match="needs --case"):
        main(["eval", "grade", "01EXP", "--app", "ai-researcher", "--case", "one"])

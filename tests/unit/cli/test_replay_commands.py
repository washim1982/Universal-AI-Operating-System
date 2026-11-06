"""``aios replay``, ``fork``, ``retry`` and ``compare``: what each asks of the server."""

import json
from typing import Any

import httpx2
import pytest

from aios.cli import client as cli_client
from aios.cli.main import main

RUN = "01RUN"
NEW = "01NEW"


class Server:
    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []
        self.compared: dict[str, Any] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        path = request.url.path
        if request.method == "POST":
            return httpx2.Response(202, json={"run_id": NEW, "app": "desk", "events": ""})
        if path.endswith("/events"):
            body = 'data: {"type": "run.completed", "time": "2026-11-02T10:00:00+00:00"}\n\n'
            return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})
        if "/compare/" in path:
            return httpx2.Response(200, json=self.compared)
        return httpx2.Response(200, json={"status": "completed", "output": "ok", "usage": {}})

    @property
    def posted(self) -> httpx2.Request:
        return next(r for r in self.seen if r.method == "POST")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    found = Server()
    original = httpx2.Client

    def client(**kwargs: Any) -> httpx2.Client:
        return original(transport=httpx2.MockTransport(found), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", client)
    return found


def test_replay_starts_one_and_follows_it(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["replay", RUN, "--app", "desk"]) == 0
    assert server.posted.url.path == f"/v1/apps/desk/runs/{RUN}/replay"
    assert f"run {NEW}" in capsys.readouterr().out


def test_fork_sends_what_changes(server: Server) -> None:
    arguments = ["fork", RUN, "--app", "desk", "--from", "pay", "--input", '{"item": "pear"}']
    arguments += ["--step-input", "just text", "--model", "anthropic/claude-haiku-5-5"]
    assert main(arguments) == 0
    assert json.loads(server.posted.content) == {
        "step": "pay",
        "input": {"item": "pear"},
        "step_input": "just text",
        "model": "anthropic/claude-haiku-5-5",
    }


def test_fork_needs_a_step(server: Server) -> None:
    with pytest.raises(SystemExit):
        main(["fork", RUN, "--app", "desk"])


def test_retry_by_default_starts_where_it_failed(server: Server) -> None:
    assert main(["retry", RUN, "--app", "desk"]) == 0
    assert server.posted.url.path == f"/v1/apps/desk/runs/{RUN}/retry"
    assert json.loads(server.posted.content) == {}


def _side(status: str, *, copied: bool = False, stubbed: int = 0) -> dict[str, Any]:
    return {"status": status, "cost_usd": 0.01, "copied": copied, "stubbed": stubbed}


def test_compare_shows_each_step(server: Server, capsys: pytest.CaptureFixture[str]) -> None:
    server.compared = {
        "same_output": False,
        "steps": [
            {
                "step": "check",
                "a": _side("completed"),
                "b": _side("completed", copied=True),
                "same_output": True,
            },
            {
                "step": "pay",
                "a": _side("completed"),
                "b": _side("completed", stubbed=1),
                "same_output": False,
            },
        ],
    }
    assert main(["compare", RUN, NEW, "--app", "desk"]) == 1  # they differ
    out = capsys.readouterr().out
    assert "copied" in out
    assert "1 stubbed" in out
    assert "DIFFERENT" in out


def test_compare_of_the_same_says_so(server: Server, capsys: pytest.CaptureFixture[str]) -> None:
    server.compared = {"same_output": True, "steps": []}
    assert main(["compare", RUN, NEW, "--app", "desk"]) == 0
    assert "run output: same" in capsys.readouterr().out

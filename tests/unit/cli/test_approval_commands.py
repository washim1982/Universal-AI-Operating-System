"""The ``aios approvals`` commands, and the server's webhook settings."""

import json
from typing import Any

import httpx2
import pytest

from aios.cli import client as cli_client
from aios.cli.main import main
from aios.cli.server import approval_webhook

APPROVAL = "01J0000000000000000000000A"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> list[httpx2.Request]:
    """Every ``Client`` talks to a recording stand-in for the API."""
    seen: list[httpx2.Request] = []
    original = httpx2.Client

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"status": "pending"})

    def client(**kwargs: Any) -> httpx2.Client:
        return original(transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", client)
    return seen


def test_list_asks_for_the_callers_inbox(
    server: list[httpx2.Request], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["approvals", "list"]) == 0
    assert server[0].url.path == "/v1/approvals"
    assert server[0].url.params["status"] == "pending"
    assert server[0].url.params["everyone"] == "false"


def test_list_all_asks_for_everyones(
    server: list[httpx2.Request], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["approvals", "list", "--all", "--status", "granted"]) == 0
    assert server[0].url.params["status"] == "granted"
    assert server[0].url.params["everyone"] == "true"


def test_show_reads_one(server: list[httpx2.Request], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["approvals", "show", APPROVAL]) == 0
    assert server[0].url.path == f"/v1/approvals/{APPROVAL}"
    assert json.loads(capsys.readouterr().out) == {"status": "pending"}


@pytest.mark.parametrize("verdict", ["approve", "deny"])
def test_an_answer_goes_to_its_own_route(
    server: list[httpx2.Request], capsys: pytest.CaptureFixture[str], verdict: str
) -> None:
    assert main(["approvals", verdict, APPROVAL, "--comment", "checked it"]) == 0
    assert server[0].method == "POST"
    assert server[0].url.path == f"/v1/approvals/{APPROVAL}/{verdict}"
    assert json.loads(server[0].content) == {"comment": "checked it"}


@pytest.mark.parametrize("action", ["show", "approve", "deny"])
def test_these_need_an_id(server: list[httpx2.Request], action: str) -> None:
    with pytest.raises(SystemExit, match="needs an approval id"):
        main(["approvals", action])
    assert server == []


def test_a_webhook_needs_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOS_APPROVAL_WEBHOOK_URL", "https://chat.example/hook")
    monkeypatch.delenv("AIOS_APPROVAL_WEBHOOK_SECRET", raising=False)
    with pytest.raises(SystemExit, match="SECRET"):
        approval_webhook()


def test_no_webhook_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOS_APPROVAL_WEBHOOK_URL", raising=False)
    assert approval_webhook() is None


def test_a_webhook_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOS_APPROVAL_WEBHOOK_URL", "https://chat.example/hook")
    monkeypatch.setenv("AIOS_APPROVAL_WEBHOOK_SECRET", "shh")
    webhook = approval_webhook()
    assert webhook is not None
    assert (webhook.url, webhook.secret) == ("https://chat.example/hook", "shh")

"""The ``aios app`` commands: what each one asks of the server, and what it refuses to guess."""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.cli import client as cli_client
from aios.cli.main import main

APP = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: ticket}]
  workflows: [ticket]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: ticket, version: 1.0.0}
spec:
  steps:
    - {id: only, type: wait, signal: go}
"""
WANTED = {
    "tools": ["mail.send", "mail.read"],
    "data_classes": ["internal", "confidential"],
    "memory_scopes": ["app", "user"],
    "hosts": ["docs.example.com"],
}


class Api:
    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []
        self.answer: dict[str, Any] = {"app": "desk", "pending": "1.0.0", "requested": WANTED}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        return httpx2.Response(200, json=self.answer)

    def body(self, index: int = -1) -> Any:
        return json.loads(self.seen[index].content)

    @property
    def routes(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.seen]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Api:
    api = Api()
    original = httpx2.Client

    def client(**kwargs: Any) -> httpx2.Client:
        return original(transport=httpx2.MockTransport(api), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", client)
    return api


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / "desk" / "manifests").mkdir(parents=True)
    (tmp_path / "desk" / "manifests" / "app.yaml").write_text(APP, "utf-8")
    return tmp_path / "desk"


def test_install_offers_and_leaves_consent_to_a_person(server: Api, folder: Path) -> None:
    assert main(["app", "install", str(folder)]) == 0
    assert server.routes == ["POST /v1/installations"]
    assert server.body()["bundle"]["name"] == "desk"


def test_install_yes_agrees_as_well(server: Api, folder: Path) -> None:
    assert main(["app", "install", str(folder), "--yes"]) == 0
    assert server.routes == ["POST /v1/installations", "POST /v1/installations/desk/consent"]
    assert server.body() == {}


def test_install_takes_configuration(server: Api, folder: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"limit": 75}', "utf-8")
    assert main(["app", "install", str(folder), "--config", str(config)]) == 0
    assert server.body()["config"] == {"limit": 75}


def test_consent_agrees_to_all_by_default(server: Api) -> None:
    assert main(["app", "consent", "desk"]) == 0
    assert server.routes == ["POST /v1/installations/desk/consent"]
    assert server.body() == {}


def test_consent_can_leave_things_out(server: Api) -> None:
    arguments = ["--deny-tool", "mail.send", "--deny-scope", "user"]
    arguments += ["--deny-class", "confidential", "--deny-host", "docs.example.com"]
    assert main(["app", "consent", "desk", *arguments]) == 0
    assert server.body()["grants"] == {
        "tools": ["mail.read"],
        "data_classes": ["internal"],
        "memory_scopes": ["app"],
        "hosts": [],
    }


def test_leaving_something_out_keeps_what_was_refused_before(server: Api) -> None:
    server.answer["refused"] = {"tools": ["mail.read"]}
    assert main(["app", "consent", "desk", "--deny-class", "confidential"]) == 0
    assert server.body()["grants"]["tools"] == ["mail.send"]


def test_taking_a_refusal_back_takes_saying_so(server: Api) -> None:
    server.answer["refused"] = {"tools": ["mail.read"]}
    assert main(["app", "consent", "desk", "--all"]) == 0
    assert server.body()["grants"]["tools"] == WANTED["tools"]


def test_consent_with_nothing_waiting_says_so(server: Api) -> None:
    server.answer = {"app": "desk"}
    with pytest.raises(SystemExit, match="isn't waiting"):
        main(["app", "consent", "desk", "--deny-tool", "mail.send"])


def test_refuse_and_show_and_list(server: Api) -> None:
    assert main(["app", "refuse", "desk"]) == 0
    assert main(["app", "show", "desk"]) == 0
    server.answer = {}
    assert main(["app", "list"]) == 0
    assert server.routes == [
        "POST /v1/installations/desk/refuse",
        "GET /v1/installations/desk",
        "GET /v1/installations",
    ]


def test_rollback_needs_a_version(server: Api) -> None:
    assert main(["app", "rollback", "desk", "1.0.0"]) == 0
    assert server.body() == {"version": "1.0.0"}
    with pytest.raises(SystemExit, match="which version"):
        main(["app", "rollback", "desk"])


def test_configure_sends_the_file(server: Api, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"limit": 10}', "utf-8")
    assert main(["app", "configure", "desk", "--config", str(config)]) == 0
    assert server.routes == ["PUT /v1/installations/desk/config"]
    assert server.body() == {"limit": 10}


def test_uninstall_wont_guess_about_data(server: Api) -> None:
    with pytest.raises(SystemExit, match="--data"):
        main(["app", "uninstall", "desk"])
    assert server.seen == []


@pytest.mark.parametrize("data", ["retain", "delete"])
def test_uninstall_says_what_happens_to_data(server: Api, data: str) -> None:
    assert main(["app", "uninstall", "desk", "--data", data]) == 0
    assert server.seen[0].url.params["data"] == data


def test_uninstall_export_saves_before_it_deletes(server: Api, tmp_path: Path) -> None:
    target = tmp_path / "saved.jsonl"
    assert main(["app", "uninstall", "desk", "--data", "export", "-o", str(target)]) == 0
    assert server.routes == [
        "GET /v1/installations/desk/export",
        "DELETE /v1/installations/desk",
    ]
    assert server.seen[1].url.params["data"] == "delete"
    assert target.exists()


def test_a_failed_export_deletes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.method)
        return httpx2.Response(500, json={"detail": "no"})

    original = httpx2.Client
    monkeypatch.setattr(
        cli_client.httpx2,
        "Client",
        lambda **kwargs: original(transport=httpx2.MockTransport(handle), **kwargs),  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
    with pytest.raises(SystemExit, match="can't export"):
        main(["app", "uninstall", "desk", "--data", "export", "-o", str(tmp_path / "x.jsonl")])
    assert seen == ["GET"]


def test_package_needs_no_server(
    folder: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "desk.json"
    assert main(["app", "package", str(folder), "-o", str(target)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["digest"].startswith("sha256:")
    assert json.loads(target.read_text("utf-8"))["digest"] == shown["digest"]


def test_a_packaged_file_can_be_installed(server: Api, folder: Path, tmp_path: Path) -> None:
    target = tmp_path / "desk.json"
    main(["app", "package", str(folder), "-o", str(target)])
    assert main(["app", "install", str(target)]) == 0
    assert server.body()["bundle"]["name"] == "desk"


def test_a_changed_file_isnt_installed(server: Api, folder: Path, tmp_path: Path) -> None:
    target = tmp_path / "desk.json"
    main(["app", "package", str(folder), "-o", str(target)])
    target.write_text(target.read_text("utf-8").replace("1.0.0", "1.0.1"), "utf-8")
    with pytest.raises(SystemExit, match="digest"):
        main(["app", "install", str(target)])
    assert server.seen == []


def test_every_action_but_list_needs_a_target(server: Api) -> None:
    with pytest.raises(SystemExit, match="of which App"):
        main(["app", "show"])


def test_push_needs_somewhere_to_go(folder: Path) -> None:
    with pytest.raises(SystemExit, match="where to"):
        main(["app", "push", str(folder)])


def test_apply_asks_unless_told_otherwise(server: Api, folder: Path) -> None:
    assert main(["apply", "-f", str(folder)]) == 0
    assert server.seen[0].url.params["consent"] == "ask"
    assert main(["apply", "-f", str(folder), "--yes"]) == 0
    assert server.seen[1].url.params["consent"] == "all"

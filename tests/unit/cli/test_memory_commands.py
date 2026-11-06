"""The ``aios memory`` commands."""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.cli import client as cli_client
from aios.cli.main import main


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> list[httpx2.Request]:
    """Every ``Client`` talks to a recording stand-in for the API."""
    seen: list[httpx2.Request] = []
    original = httpx2.Client

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"added": 1})

    def client(**kwargs: Any) -> httpx2.Client:
        return original(transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", client)
    return seen


def test_search_sends_the_query_and_scopes(
    server: list[httpx2.Request], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["memory", "search", "refunds", "--scope", "app", "-k", "3"]) == 0
    assert server[0].url.path == "/v1/memory/search"
    assert json.loads(server[0].content) == {"query": "refunds", "scopes": ["app"], "k": 3}


def test_get_reads_one_item(
    server: list[httpx2.Request], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["memory", "get", "ITEM"]) == 0
    assert server[0].url.path == "/v1/memory/ITEM"


def test_forget_takes_a_subject_or_an_item(
    server: list[httpx2.Request], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["memory", "forget", "--subject", "user:alice@acme"]) == 0
    assert json.loads(server[0].content) == {"item": None, "subject": "user:alice@acme"}
    with pytest.raises(SystemExit, match="--item or --subject"):
        main(["memory", "forget"])


def test_ingesting_a_folder_uploads_its_text(
    server: list[httpx2.Request], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI reads the files, because the server must never be told a path to open."""
    folder = tmp_path / "kb"
    folder.mkdir()
    (folder / "a.md").write_text("Refunds take five days.", "utf-8")
    assert main(["memory", "ingest", str(folder), "--app", "desk"]) == 0
    assert (server[0].method, server[0].url.path) == ("PUT", "/v1/apps/desk/knowledge")
    assert json.loads(server[0].content) == {
        "source": "kb/",
        "documents": [{"uri": "kb/a.md", "text": "Refunds take five days."}],
        "scope": "app",
        "sensitivity": "internal",
        "prune": True,
    }


def test_keep_leaves_other_documents_alone(
    server: list[httpx2.Request], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.md").write_text("x", "utf-8")
    main(["memory", "ingest", str(tmp_path), "--app", "desk", "--keep"])
    assert json.loads(server[0].content)["prune"] is False


def test_a_url_is_left_for_the_server_to_fetch_through_its_allowlist(
    server: list[httpx2.Request], capsys: pytest.CaptureFixture[str]
) -> None:
    url = "https://docs.acme.test/refunds"
    assert main(["memory", "ingest", url, "--app", "desk", "--sensitivity", "confidential"]) == 0
    assert server[0].url.path == "/v1/apps/desk/knowledge/url"
    assert json.loads(server[0].content) == {
        "url": url,
        "scope": "app",
        "sensitivity": "confidential",
    }


def test_ingesting_needs_an_app(server: list[httpx2.Request], tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="needs --app"):
        main(["memory", "ingest", str(tmp_path)])


@pytest.mark.parametrize("action", ["search", "get", "ingest"])
def test_most_actions_need_a_target(server: list[httpx2.Request], action: str) -> None:
    with pytest.raises(SystemExit, match="needs a target"):
        main(["memory", action])

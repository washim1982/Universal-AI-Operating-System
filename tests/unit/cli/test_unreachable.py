"""When nothing answers at the server's address, ``aios`` says so in one line, not a traceback."""

from typing import Any

import httpx2
import pytest

from aios.cli import client as cli_client
from aios.cli.main import main


@pytest.fixture
def nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every request is refused, as when no AIOS is running at that port."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("No connection could be made", request=request)

    original = httpx2.Client

    def client(**kwargs: Any) -> httpx2.Client:
        return original(transport=httpx2.MockTransport(refuse), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", client)


@pytest.mark.parametrize(
    "command",
    [
        ["get", "apps"],
        ["run", "ai-researcher", "hello"],  # streams its events
        ["approvals", "list"],
    ],
)
@pytest.mark.usefixtures("nobody")
def test_nothing_answering_says_where_it_looked(command: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main([*command, "--server", "http://127.0.0.1:18080"])
    said = str(stopped.value)
    assert said.startswith("no answer from AIOS at http://127.0.0.1:18080")
    assert "Is it running? Check --server or AIOS_SERVER" in said

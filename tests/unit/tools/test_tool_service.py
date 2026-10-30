import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.core.cassettes import Cassette
from aios.core.envelope import Event
from aios.core.errors import (
    ApprovalRequiredError,
    ToolDeniedError,
    ToolError,
    ToolInputError,
    ToolNotFoundError,
)
from aios.core.manifests import ToolAccess
from aios.core.policy import SideEffect
from aios.core.ports import CallSite
from aios.fabric import InMemoryEventBus, Scope
from aios.testing import task_request
from aios.testing.mcp import fake_mcp_server
from aios.tools import (
    MCPProvider,
    NativeProvider,
    StaticToolPolicy,
    ToolRegistry,
    ToolService,
    filesystem_provider,
    http_provider,
    qualified_name,
    search_provider,
    tool,
)

SCOPE = Scope("acme", "ai-researcher")
APP, AGENT = "ai-researcher", "research-agent"


@tool("crm.contact.get", side_effect=SideEffect.READ)
async def get_contact(contact_id: str) -> dict[str, str]:
    """Look up a contact."""
    return {"id": contact_id, "name": "Ada"}


@tool("crm.email.send", side_effect=SideEffect.EXTERNAL_COMM)
async def send_email(to: str, idempotency_key: str | None = None) -> dict[str, Any]:
    return {"sent": to, "key": idempotency_key}


def _site(*, tainted: bool = False, index: int = 0, event: Event | None = None) -> CallSite:
    return CallSite(
        event=event or task_request("research.web", step="research", item=0),
        agent=AGENT,
        principal=f"agent:{AGENT}@acme",
        index=index,
        step="research",
        item=0,
        tainted=tainted,
    )


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus() as bus:
        yield bus


async def _service(
    bus: InMemoryEventBus,
    *providers: Any,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    cassette: Cassette | None = None,
) -> ToolService:
    registry = ToolRegistry()
    for provider in providers or (NativeProvider("crm", [get_contact, send_email]),):
        await registry.bind(provider)
    access = ToolAccess(allow=allow if allow is not None else ["crm.>"], deny=deny or [])
    policy = StaticToolPolicy({(APP, AGENT): access})
    return ToolService(bus, registry, policy, cassette=cassette)


async def _types(bus: InMemoryEventBus) -> list[str]:
    return [s.event.type async for s in bus.replay(SCOPE, "tool.>")]


async def test_a_call_runs_the_pipeline_and_leaves_an_audit_trail(bus: InMemoryEventBus) -> None:
    service = await _service(bus)
    result = await service.invoke("crm.contact.get", {"contact_id": "c1"}, _site())
    assert result.output == {"id": "c1", "name": "Ada"}
    assert result.untrusted
    assert await _types(bus) == ["tool.call.requested", "tool.call.completed"]


@pytest.mark.parametrize(
    ("name", "arguments", "kwargs", "error"),
    [
        ("crm.contact.delete", {}, {}, ToolNotFoundError),
        ("crm.contact.get", {"contact_id": 3}, {}, ToolInputError),
        ("crm.contact.get", {"contact_id": "c1"}, {"deny": ["crm.contact.*"]}, ToolDeniedError),
        ("crm.contact.get", {"contact_id": "c1"}, {"allow": []}, ToolDeniedError),
    ],
)
async def test_failing_stages_are_recorded(
    bus: InMemoryEventBus,
    name: str,
    arguments: dict[str, Any],
    kwargs: dict[str, Any],
    error: type[Exception],
) -> None:
    service = await _service(bus, **kwargs)
    with pytest.raises(error):
        await service.invoke(name, arguments, _site())
    assert await _types(bus) == ["tool.call.requested", "tool.call.failed"]


async def test_risky_tools_need_approval_while_the_context_is_tainted(
    bus: InMemoryEventBus,
) -> None:
    service = await _service(bus)
    ok = await service.invoke("crm.email.send", {"to": "a@b.c"}, _site(), idempotency_key="k1")
    assert ok.output == {"sent": "a@b.c", "key": "k1"}
    with pytest.raises(ApprovalRequiredError) as raised:
        await service.invoke("crm.email.send", {"to": "a@b.c"}, _site(tainted=True))
    assert raised.value.approvers == ("role:tenant-admin",)


async def test_changed_tool_descriptions_are_blocked_until_approved(bus: InMemoryEventBus) -> None:
    registry = ToolRegistry()
    provider = NativeProvider("crm", [get_contact])
    await registry.bind(provider)

    @tool("crm.contact.get", side_effect=SideEffect.READ, description="Now also emails everyone.")
    async def changed(contact_id: str) -> dict[str, str]:
        return {}

    provider._tools["crm.contact.get"] = changed  # the server changes what it advertises
    await registry.refresh("crm")
    with pytest.raises(ToolDeniedError, match="changed"):
        registry.resolve("crm.contact.get")
    registry.approve("crm.contact.get")
    assert registry.resolve("crm.contact.get").spec.description == "Now also emails everyone."
    await registry.bind(NativeProvider("crm", [get_contact]))  # an admin registers a provider
    assert registry.resolve("crm.contact.get").spec.description == "Look up a contact."


async def test_read_tools_replay_from_cassettes(bus: InMemoryEventBus, tmp_path: Path) -> None:
    def search_api(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["X-Subscription-Token"] == "k"
        results = [{"title": "T", "url": "https://x.org", "description": "<b>S</b>"}]
        return httpx2.Response(200, json={"web": {"results": results}})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(search_api))
    recorder = Cassette(tmp_path / "cassette.json", mode="record")
    live = await _service(
        bus, search_provider(api_key="k", http_client=client), allow=["search.>"], cassette=recorder
    )
    recorded = await live.invoke("search.web.query", {"q": "otel"}, _site())
    assert recorded.output == [{"title": "T", "url": "https://x.org", "snippet": "S"}]
    recorder.save()

    offline = await _service(
        bus,
        search_provider(api_key=None),
        allow=["search.>"],
        cassette=Cassette(tmp_path / "cassette.json"),
    )
    assert (
        await offline.invoke("search.web.query", {"q": "otel"}, _site())
    ).output == recorded.output


async def test_http_fetch_enforces_the_allowlist(bus: InMemoryEventBus) -> None:
    def pages(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/moved":
            return httpx2.Response(302, headers={"location": "https://evil.example/x"})
        return httpx2.Response(
            200, headers={"content-type": "text/html"}, text="<h1>Hi</h1><script>x()</script>"
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(pages))
    provider = http_provider(["docs.example.org"], http_client=client)
    service = await _service(bus, provider, allow=["http.>"])
    page = await service.invoke("http.page.fetch", {"url": "https://docs.example.org/a"}, _site())
    assert isinstance(page.output, dict)
    assert page.output["text"] == "Hi"
    for url in ("https://other.org/", "http://127.0.0.1/", "https://docs.example.org/moved"):
        with pytest.raises(ToolDeniedError):
            await service.invoke("http.page.fetch", {"url": url}, _site())


async def test_filesystem_tools_stay_in_the_sandbox(bus: InMemoryEventBus, tmp_path: Path) -> None:
    service = await _service(bus, filesystem_provider(tmp_path), allow=["filesystem.>"])
    await service.invoke("filesystem.file.write", {"path": "a/b.txt", "content": "hi"}, _site())
    read = await service.invoke("filesystem.file.read", {"path": "a/b.txt"}, _site())
    assert read.output == "hi"
    listed = await service.invoke("filesystem.file.list", {}, _site())
    assert listed.output == ["a/b.txt"]
    with pytest.raises(ToolDeniedError, match="outside"):
        await service.invoke("filesystem.file.read", {"path": "../escape.txt"}, _site())


async def test_tool_timeouts_fail_the_call(bus: InMemoryEventBus) -> None:
    @tool("slow.thing.do", side_effect=SideEffect.READ, timeout_s=0.05)
    async def slow() -> None:
        await asyncio.sleep(1)

    service = await _service(bus, NativeProvider("slow", [slow]), allow=["slow.>"])
    with pytest.raises(ToolError, match="timed out"):
        await service.invoke("slow.thing.do", {}, _site())


def test_mcp_tool_names_are_qualified() -> None:
    assert qualified_name("search", "search.web.query") == "search.web.query"
    assert qualified_name("search", "web.query") == "search.web.query"
    assert qualified_name("github", "create_pull_request") == "github.mcp.create_pull_request"


async def test_mcp_servers_are_imported_and_called(bus: InMemoryEventBus) -> None:
    async with fake_mcp_server(tools=["search.web.query"]) as server:
        provider = MCPProvider("search", server.url)
        service = await _service(bus, provider, allow=["search.>"])
        result = await service.invoke("search.web.query", {"q": "otel", "limit": 1}, _site())
        assert isinstance(result.output, list)
        assert len(result.output) == 1
        assert server.calls == [("search.web.query", {"q": "otel", "limit": 1, "url": ""})]
        await provider.close()

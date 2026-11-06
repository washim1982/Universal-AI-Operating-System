"""Where a tool call may send data (FR-SEC-10)."""

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from aios.core.errors import EgressDeniedError, ToolDeniedError, ToolInputError
from aios.core.manifests import AppManifest, parse_manifest
from aios.core.manifests import Egress as EgressSpec
from aios.core.policy import AllowAll, SideEffect
from aios.core.ports import CallSite
from aios.fabric import InMemoryEventBus, Scope
from aios.testing import task_request
from aios.tools import MCPProvider, NativeProvider, ToolRegistry, ToolService, http_provider, tool
from aios.tools.builtins import search_provider
from aios.tools.egress import Egress, host_of, listed, urls_in

TENANT, APP = "acme", "ai-researcher"


# --------------------------------------------------------------------------- reading a URL


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://example.com/a?b=c", "example.com"),
        ("HTTP://EXAMPLE.COM/", "example.com"),
        ("https://example.com./x", "example.com"),
        ("https://example.com:8443/x", "example.com"),
    ],
)
def test_a_host_is_read_from_a_url(url: str, host: str) -> None:
    assert host_of(url) == host


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/",
        "http://[::1]:8080/",
        "http://[fd00::1]/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0x7f.0.0.1/",
        "http://017700000001/",
        "http://127.1/",
    ],
)
def test_an_address_is_refused_however_it_is_written(url: str) -> None:
    with pytest.raises(ToolDeniedError, match="IP addresses"):
        host_of(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example\\@docs.example.com/",
        "https://docs.example.com\\.evil.example/",
        "https://docs.example.com@evil.example/",
        "https://user:pass@docs.example.com/",
        "https://docs.example.com/a b",
        "https://docs.example.com/\nHost: evil.example",
        "https://[not-an-address/",
    ],
)
def test_a_url_two_parsers_could_read_differently_is_refused(url: str) -> None:
    with pytest.raises(ToolDeniedError):
        host_of(url)


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "gopher://x/", "/rel"])
def test_only_web_urls_count(url: str) -> None:
    with pytest.raises(ToolInputError):
        host_of(url)


def test_urls_are_found_anywhere_in_the_arguments() -> None:
    arguments: dict[str, Any] = {
        "text": "see https://a.example/x and (https://b.example/y).",
        "nested": [{"deep": {"href": "http://c.example"}}],
        "image": "![x](https://d.example/p?q=1)",
        "number": 7,
        "nothing": None,
    }
    hosts = {host_of(url) for url in urls_in(arguments)}
    assert hosts == {"a.example", "b.example", "c.example", "d.example"}


def test_text_with_no_urls_has_no_hosts() -> None:
    assert list(urls_in({"text": "write to sales at example dot com"})) == []


# --------------------------------------------------------------------------- lists


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("example.com", True),
        ("docs.example.com", True),
        ("a.b.example.com", True),
        ("evilexample.com", False),
        ("example.com.evil.example", False),
        ("example.org", False),
    ],
)
def test_a_name_covers_what_is_below_it(host: str, expected: bool) -> None:
    assert listed(host, ["example.com"]) is expected


def test_with_no_list_at_all_nothing_is_reachable() -> None:
    assert not Egress().permits("example.com")


def test_a_host_has_to_be_on_every_list_that_exists() -> None:
    egress = Egress(app=("a.example", "b.example"), tool=("b.example", "c.example"))
    assert not egress.permits("a.example")
    assert egress.permits("b.example")
    assert not egress.permits("c.example")
    assert not egress.permits("b.example", ["c.example"])
    assert egress.permits("b.example", ["b.example"])


def test_an_app_can_declare_that_it_reaches_nowhere() -> None:
    egress = Egress(app=())
    assert egress.declared
    assert not egress.permits("example.com")


def test_an_installation_list_works_alone() -> None:
    assert Egress().permits("example.com", ["example.com"])


# --------------------------------------------------------------------------- the manifest


def _app(**egress: Any) -> AppManifest:
    manifest = parse_manifest(
        {
            "apiVersion": "aios/v1alpha1",
            "kind": "App",
            "metadata": {"name": "a", "version": "1.0.0"},
            "spec": {
                "entrypoints": [{"type": "chat", "workflow": "w"}],
                "workflows": ["w"],
                "egress": egress,
            },
        }
    )
    assert isinstance(manifest, AppManifest)
    return manifest


def test_an_app_declares_its_hosts() -> None:
    spec = _app(allow=["docs.example.com"], tools={"http.page.fetch": ["docs.example.com"]})
    assert spec.spec.egress.allow == ["docs.example.com"]
    assert spec.spec.egress.tools == {"http.page.fetch": ["docs.example.com"]}


def test_no_list_isnt_an_empty_list() -> None:
    assert _app().spec.egress.allow is None
    assert _app(allow=[]).spec.egress.allow == []


@pytest.mark.parametrize("host", ["*", "*.example.com", "https://example.com", "EXAMPLE.com"])
def test_a_host_is_a_plain_name(host: str) -> None:
    with pytest.raises(ValueError, match="pattern"):
        _app(allow=[host])


# --------------------------------------------------------------------------- the pipeline


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus() as bus:
        yield bus


def _site() -> CallSite:
    event = task_request("x", step="s", tenantid=TENANT, appid=APP)
    return CallSite(event=event, agent="a", principal="agent:a@acme", index=0, step="s")


class Sent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def client(self) -> httpx2.AsyncClient:
        def answer(request: httpx2.Request) -> httpx2.Response:
            self.calls.append(str(request.url))
            if request.url.path == "/away":
                return httpx2.Response(302, headers={"location": "https://evil.example/stolen"})
            return httpx2.Response(200, text="page", headers={"content-type": "text/plain"})

        return httpx2.AsyncClient(transport=httpx2.MockTransport(answer))


@tool("note.message.post", side_effect=SideEffect.WRITE)
async def post(text: str) -> dict[str, str]:
    return {"text": text}


async def _service(
    bus: InMemoryEventBus, spec: EgressSpec, *providers: NativeProvider
) -> ToolService:
    registry = ToolRegistry()
    for provider in providers:
        await registry.bind(provider)
    service = ToolService(bus, registry, AllowAll())
    service.set_egress(TENANT, APP, spec)
    return service


async def test_a_listed_host_can_be_fetched(bus: InMemoryEventBus) -> None:
    sent = Sent()
    provider = http_provider([], http_client=sent.client())
    service = await _service(bus, EgressSpec(allow=["docs.example.com"]), provider)
    await service.invoke("http.page.fetch", {"url": "https://docs.example.com/a"}, _site())
    assert sent.calls == ["https://docs.example.com/a"]


async def test_an_app_list_stands_without_an_installation_list(bus: InMemoryEventBus) -> None:
    sent = Sent()
    provider = http_provider([], http_client=sent.client())
    service = await _service(bus, EgressSpec(), provider)
    with pytest.raises(ToolDeniedError, match="allowlist"):
        await service.invoke("http.page.fetch", {"url": "https://docs.example.com/a"}, _site())
    assert sent.calls == []


async def test_an_app_list_narrows_the_installation_list(bus: InMemoryEventBus) -> None:
    sent = Sent()
    provider = http_provider(["docs.example.com", "evil.example"], http_client=sent.client())
    service = await _service(bus, EgressSpec(allow=["docs.example.com"]), provider)
    with pytest.raises(EgressDeniedError, match=r"evil\.example"):
        await service.invoke("http.page.fetch", {"url": "https://evil.example/a"}, _site())
    assert sent.calls == []


async def test_the_installation_list_is_a_ceiling_on_the_app(bus: InMemoryEventBus) -> None:
    """An App can't reach what the operator closed, by listing it."""
    sent = Sent()
    provider = http_provider(["docs.example.com"], http_client=sent.client())
    service = await _service(bus, EgressSpec(allow=["evil.example"]), provider)
    with pytest.raises(ToolDeniedError, match="allowlist"):
        await service.invoke("http.page.fetch", {"url": "https://evil.example/a"}, _site())
    assert sent.calls == []


async def test_a_tool_can_have_a_list_of_its_own(bus: InMemoryEventBus) -> None:
    sent = Sent()
    provider = http_provider([], http_client=sent.client())
    spec = EgressSpec(allow=["a.example", "b.example"], tools={"http.page.fetch": ["a.example"]})
    service = await _service(bus, spec, provider)
    await service.invoke("http.page.fetch", {"url": "https://a.example/"}, _site())
    with pytest.raises(EgressDeniedError):
        await service.invoke("http.page.fetch", {"url": "https://b.example/"}, _site())


async def test_a_redirect_is_checked_against_the_apps_list(bus: InMemoryEventBus) -> None:
    sent = Sent()
    provider = http_provider(["docs.example.com", "evil.example"], http_client=sent.client())
    service = await _service(bus, EgressSpec(allow=["docs.example.com"]), provider)
    with pytest.raises(ToolDeniedError, match=r"evil\.example"):
        await service.invoke("http.page.fetch", {"url": "https://docs.example.com/away"}, _site())
    assert sent.calls == ["https://docs.example.com/away"]


async def test_a_link_in_a_message_is_checked_too(bus: InMemoryEventBus) -> None:
    provider = NativeProvider("note", [post])
    service = await _service(bus, EgressSpec(allow=["example.com"]), provider)
    await service.invoke("note.message.post", {"text": "see https://example.com/a"}, _site())
    with pytest.raises(EgressDeniedError, match=r"evil\.example"):
        await service.invoke("note.message.post", {"text": "see https://evil.example/a"}, _site())


async def test_without_a_list_a_message_can_carry_links(bus: InMemoryEventBus) -> None:
    provider = NativeProvider("note", [post])
    service = await _service(bus, EgressSpec(), provider)
    await service.invoke("note.message.post", {"text": "see https://anywhere.example"}, _site())


async def test_a_refusal_is_a_recorded_decision(bus: InMemoryEventBus) -> None:
    provider = NativeProvider("note", [post])
    service = await _service(bus, EgressSpec(allow=[]), provider)
    with pytest.raises(EgressDeniedError):
        await service.invoke("note.message.post", {"text": "https://evil.example"}, _site())
    [stored] = [s async for s in bus.replay(Scope(TENANT), "policy.decision.*")]
    assert stored.event.type == "policy.decision.denied"
    assert isinstance(stored.event.data, dict)
    assert stored.event.data["policies"] == ["egress"]
    assert "evil.example" in str(stored.event.data["reason"])


async def test_a_malformed_url_is_refused_before_anything_is_sent(bus: InMemoryEventBus) -> None:
    sent = Sent()
    provider = http_provider(["docs.example.com"], http_client=sent.client())
    service = await _service(bus, EgressSpec(allow=["docs.example.com"]), provider)
    with pytest.raises(EgressDeniedError):
        await service.invoke(
            "http.page.fetch", {"url": "https://docs.example.com@evil.example/"}, _site()
        )
    assert sent.calls == []


async def test_a_fixed_host_counts_once_an_app_has_a_list(bus: InMemoryEventBus) -> None:
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json={"web": {}}))
    )
    provider = search_provider(api_key="k", backend="brave", http_client=client)
    service = await _service(bus, EgressSpec(allow=["docs.example.com"]), provider)
    with pytest.raises(EgressDeniedError, match=r"api\.search\.brave\.com"):
        await service.invoke("search.web.query", {"q": "a secret"}, _site())


async def test_search_needs_no_list_when_the_app_has_none(bus: InMemoryEventBus) -> None:
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json={"web": {}}))
    )
    provider = search_provider(api_key="k", backend="brave", http_client=client)
    service = await _service(bus, EgressSpec(), provider)
    await service.invoke("search.web.query", {"q": "a question"}, _site())


def test_an_mcp_server_is_a_destination() -> None:
    provider = MCPProvider("zendesk", "https://mcp.zendesk.example/sse")
    assert provider._reaches == ("mcp.zendesk.example",)  # pyright: ignore[reportPrivateUsage]


def test_a_local_mcp_server_goes_nowhere() -> None:
    assert MCPProvider("x", "") is not None

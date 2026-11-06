import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.controlplane import Runtime
from aios.core.cassettes import Cassette
from aios.core.catalog import ToolCallFailed
from aios.core.envelope import Event
from aios.core.errors import (
    ApprovalRequiredError,
    ToolCredentialError,
    ToolDeniedError,
    ToolError,
    ToolInputError,
    ToolNotFoundError,
)
from aios.core.manifests import ToolAccess, load_manifests
from aios.core.policy import SideEffect
from aios.core.ports import CallSite, SecretStore
from aios.fabric import InMemoryEventBus, Scope
from aios.security.enforcement import Enforcer
from aios.security.secrets import EnvSecrets
from aios.security.store import InMemorySecurityStore
from aios.testing import task_request
from aios.testing.mcp import fake_mcp_server
from aios.tools import (
    MCPProvider,
    NativeProvider,
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
    secrets: SecretStore | None = None,
) -> ToolService:
    registry = ToolRegistry()
    for provider in providers or (NativeProvider("crm", [get_contact, send_email]),):
        await registry.bind(provider)
    access = ToolAccess(allow=allow if allow is not None else ["crm.>"], deny=deny or [])
    policy = Enforcer(InMemorySecurityStore())
    policy.install("acme", APP, policies=(), agents={AGENT: access})
    return ToolService(bus, registry, policy, cassette=cassette, secrets=secrets)


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


async def test_keenable_search_trims_results_to_fit_a_small_context(
    bus: InMemoryEventBus,
) -> None:
    def search_api(request: httpx2.Request) -> httpx2.Response:
        assert (request.method, request.headers["X-API-Key"]) == ("POST", "keen_XXXXXX")
        assert json.loads(request.content) == {"query": "otel"}
        article = {
            "title": "T",
            "url": "https://x.org",
            "description": "",
            "snippet": "word " * 500,
        }
        return httpx2.Response(200, json={"mode": "pro", "results": [article] * 8})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(search_api))
    provider = search_provider(api_key="keen_XXXXXX", backend="keenable", http_client=client)
    service = await _service(bus, provider, allow=["search.>"])
    hits = (await service.invoke("search.web.query", {"q": "otel", "limit": 3}, _site())).output
    assert isinstance(hits, list)
    assert len(hits) == 3
    for hit in hits:
        assert isinstance(hit, dict)
        assert len(str(hit["snippet"])) == 1_000


def test_an_unknown_search_provider_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOS_SEARCH_PROVIDER", "altavista")
    with pytest.raises(ValueError, match="altavista"):
        search_provider()


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (422, False)])
async def test_search_errors_say_whether_to_retry(
    bus: InMemoryEventBus, status: int, retryable: bool
) -> None:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _: httpx2.Response(status)))
    service = await _service(
        bus, search_provider(api_key="k", http_client=client), allow=["search.>"]
    )
    with pytest.raises(ToolError, match=f"search API {status}") as raised:
        await service.invoke("search.web.query", {"q": "otel"}, _site())
    assert raised.value.retryable is retryable


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


# --------------------------------------------------------------------------- credentials

SECRET = "XXXXXX"


class Vault(SecretStore):
    def __init__(self, **secrets: str) -> None:
        self.secrets = secrets
        self.reads: list[tuple[str, str, str]] = []

    async def get(self, tenant: str, app: str, name: str) -> str | None:
        self.reads.append((tenant, app, name))
        return self.secrets.get(name)


def _billing(behaviour: Any = None, *, replayable: bool = False) -> NativeProvider:
    @tool("billing.refund.create", side_effect=SideEffect.WRITE, credentials=["BILLING_KEY"])
    async def refund(amount: int, credentials: Mapping[str, str]) -> Any:
        if behaviour is not None:
            return behaviour(credentials["BILLING_KEY"])
        return {"refunded": amount, "key_length": len(credentials["BILLING_KEY"])}

    return NativeProvider("billing", [refund], replayable=replayable)


async def _log(bus: InMemoryEventBus) -> str:
    return json.dumps([s.event.model_dump(mode="json") async for s in bus.replay(SCOPE)])


async def test_a_tool_gets_its_credential_and_nothing_else_does(bus: InMemoryEventBus) -> None:
    vault = Vault(BILLING_KEY=SECRET)
    service = await _service(bus, _billing(), allow=["billing.>"], secrets=vault)
    result = await service.invoke("billing.refund.create", {"amount": 5}, _site())
    assert result.output == {"refunded": 5, "key_length": len(SECRET)}
    assert vault.reads == [("acme", APP, "BILLING_KEY")]
    assert SECRET not in await _log(bus)
    [spec] = service.registry.specs()
    assert "credentials" not in spec.input_schema["properties"]  # the model can't see or fill it


async def test_a_denied_call_never_touches_the_secret(bus: InMemoryEventBus) -> None:
    vault = Vault(BILLING_KEY=SECRET)
    service = await _service(bus, _billing(), allow=[], secrets=vault)
    with pytest.raises(ToolDeniedError):
        await service.invoke("billing.refund.create", {"amount": 5}, _site())
    assert vault.reads == []


async def test_a_missing_credential_fails_before_the_tool_runs(bus: InMemoryEventBus) -> None:
    def explode(_: str) -> None:
        raise AssertionError("the tool must not run")

    service = await _service(bus, _billing(explode), allow=["billing.>"], secrets=Vault())
    with pytest.raises(ToolCredentialError, match="needs the secret BILLING_KEY"):
        await service.invoke("billing.refund.create", {"amount": 5}, _site())
    [failed] = [s.event async for s in bus.replay(SCOPE, "tool.call.failed")]
    assert ToolCallFailed.model_validate(failed.data).error.code == "tool.credential_missing"


async def test_a_tool_that_echoes_its_credential_cannot_leak_it(bus: InMemoryEventBus) -> None:
    def echoing(key: str) -> dict[str, Any]:
        return {"debug": {"headers": [f"Authorization: Bearer {key}"]}, key: "as a key"}

    vault = Vault(BILLING_KEY=SECRET)
    service = await _service(bus, _billing(echoing), allow=["billing.>"], secrets=vault)
    result = await service.invoke("billing.refund.create", {"amount": 5}, _site())
    assert result.output == {
        "debug": {"headers": ["Authorization: Bearer [REDACTED]"]},
        "[REDACTED]": "as a key",
    }
    assert SECRET not in await _log(bus)


async def test_an_error_that_quotes_a_credential_cannot_leak_it(bus: InMemoryEventBus) -> None:
    def failing(key: str) -> None:
        raise RuntimeError(f"401 for key {key}")

    vault = Vault(BILLING_KEY=SECRET)
    service = await _service(bus, _billing(failing), allow=["billing.>"], secrets=vault)
    with pytest.raises(ToolError) as caught:
        await service.invoke("billing.refund.create", {"amount": 5}, _site())
    assert str(caught.value) == "billing.refund.create: 401 for key [REDACTED]"
    assert caught.value.retryable
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert SECRET not in await _log(bus)


async def test_replay_needs_no_credential_and_recording_keeps_none(
    bus: InMemoryEventBus, tmp_path: Path
) -> None:
    path = tmp_path / "cassette.json"

    def echoing(key: str) -> dict[str, str]:
        return {"auth": key}

    provider = _billing(echoing, replayable=True)
    recorder = Cassette(path, "record")
    vault = Vault(BILLING_KEY=SECRET)
    service = await _service(bus, provider, allow=["billing.>"], cassette=recorder, secrets=vault)
    await service.invoke("billing.refund.create", {"amount": 5}, _site())
    recorder.save()
    assert SECRET not in path.read_text(encoding="utf-8")

    async with InMemoryEventBus() as fresh:
        empty = Vault()
        player = Cassette(path, "replay")
        service = await _service(
            fresh, provider, allow=["billing.>"], cassette=player, secrets=empty
        )
        result = await service.invoke("billing.refund.create", {"amount": 5}, _site())
    assert result.output == {"auth": "[REDACTED]"}
    assert empty.reads == []


def test_a_tool_that_names_credentials_must_accept_them() -> None:
    with pytest.raises(TypeError, match="no 'credentials' parameter"):

        @tool("billing.refund.create", side_effect=SideEffect.WRITE, credentials=["BILLING_KEY"])
        async def refund(amount: int) -> int:
            return amount


async def test_environment_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOS_SECRET_BILLING_KEY", SECRET)
    monkeypatch.setenv("AIOS_SECRET_EMPTY", "")
    secrets = EnvSecrets()
    assert await secrets.get("acme", APP, "BILLING_KEY") == SECRET
    assert await secrets.get("acme", APP, "EMPTY") is None
    assert await secrets.get("acme", APP, "UNSET") is None


DECLARES_A_SECRET = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: billing, version: 0.1.0}
spec:
  entrypoints: [{type: chat, workflow: wait}]
  workflows: [wait]
  config: {secrets: [BILLING_KEY]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: wait, version: 0.1.0}
spec:
  steps:
    - {id: reply, type: wait, signal: reply}
"""


async def test_an_app_only_gets_the_secrets_it_declares(bus: InMemoryEventBus) -> None:
    runtime = Runtime(bus, secrets=Vault(BILLING_KEY=SECRET, OTHER_KEY="other"), tool_providers=[])
    runtime.install(load_manifests(DECLARES_A_SECRET))
    secrets = runtime.tools.secrets
    assert await secrets.get("local", "billing", "BILLING_KEY") == SECRET
    assert await secrets.get("local", "billing", "OTHER_KEY") is None
    assert await secrets.get("local", "not-installed", "BILLING_KEY") is None
    assert await secrets.get("another-tenant", "billing", "BILLING_KEY") is None


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

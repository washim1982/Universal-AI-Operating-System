"""Memory through the API: search, erase and ingest (FR-CP-01, FR-MEM-07, 10)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.api import create_app
from aios.core.errors import ToolDeniedError, ToolError, ToolInputError
from aios.core.identity import Identity
from aios.fabric import Scope
from aios.security.identity import ApiKeys
from aios.testing.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "apps" / "ai-researcher"
NAME = "ai-researcher"
OPERATOR = "system:platform"
KNOWLEDGE = f"/v1/apps/{NAME}/knowledge"
DOCUMENT = {"uri": "kb/a.md", "text": "Refunds take five days."}


class Api:
    def __init__(self, http: httpx2.AsyncClient, runtime: LocalRuntime) -> None:
        self.http = http
        self.runtime = runtime

    async def bearer(self, *roles: str, tenant: str = "acme", name: str = "k") -> dict[str, str]:
        _, value = await self.runtime.runtime.create_key(
            Identity(OPERATOR, tenant), name, list(roles)
        )
        return {"Authorization": f"Bearer {value}"}

    async def ingest(self, headers: dict[str, str], **body: Any) -> httpx2.Response:
        payload = {"source": "kb/", "documents": [DOCUMENT], **body}
        return await self.http.put(KNOWLEDGE, headers=headers, json=payload)


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with LocalRuntime.from_app(APP, tenants=["acme", "globex"]) as runtime:
        app = create_app(runtime.runtime, authenticator=ApiKeys(runtime.runtime.security))
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
            yield Api(http, runtime)


async def test_a_document_can_be_ingested_and_found(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    report = await api.ingest(admin)
    assert report.status_code == 200
    assert report.json()["added"] == 1
    found = await api.http.post("/v1/memory/search", headers=admin, json={"query": "refunds"})
    assert found.status_code == 200
    [hit] = found.json()
    assert hit["content"] == "Refunds take five days."
    assert hit["provenance"]["source_uri"] == "kb/a.md"
    assert hit["score"] > 0


async def test_an_item_can_be_read_by_id(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    await api.ingest(admin)
    [hit] = (
        await api.http.post("/v1/memory/search", headers=admin, json={"query": "refunds"})
    ).json()
    found = await api.http.get(f"/v1/memory/{hit['id']}", headers=admin)
    assert found.status_code == 200
    assert found.json()["content"] == "Refunds take five days."


async def _found(api: Api, headers: dict[str, str]) -> dict[str, Any]:
    await api.ingest(headers)
    search = {"query": "refunds"}
    [hit] = (await api.http.post("/v1/memory/search", headers=headers, json=search)).json()
    return hit


async def test_an_item_can_be_corrected(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    hit = await _found(api, admin)
    body = {"content": "Refunds take three days.", "version": hit["version"]}
    changed = await api.http.put(f"/v1/memory/{hit['id']}", headers=admin, json=body)
    assert changed.status_code == 200, changed.text
    assert (changed.json()["content"], changed.json()["version"]) == (body["content"], 2)
    found = await api.http.get(f"/v1/memory/{hit['id']}", headers=admin)
    assert found.json()["content"] == "Refunds take three days."


async def test_a_correction_is_on_the_record(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    hit = await _found(api, admin)
    body = {"content": "Refunds take three days.", "version": hit["version"]}
    await api.http.put(f"/v1/memory/{hit['id']}", headers=admin, json=body)
    events = [
        stored.event
        async for stored in api.runtime.bus.replay(Scope("acme"), "memory.item.updated")
    ]
    [updated] = events
    assert isinstance(updated.data, dict)
    assert (updated.data["item"], updated.data["version"]) == (hit["id"], 2)
    assert updated.principal.startswith("service:")


async def test_two_people_cant_overwrite_each_other(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    hit = await _found(api, admin)
    first = {"content": "Three days.", "version": hit["version"]}
    second = {"content": "Ten days.", "version": hit["version"]}
    assert (
        await api.http.put(f"/v1/memory/{hit['id']}", headers=admin, json=first)
    ).status_code == 200
    late = await api.http.put(f"/v1/memory/{hit['id']}", headers=admin, json=second)
    assert late.status_code == 409


async def test_correcting_nothing(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    body = {"content": "x", "version": 1}
    missing = await api.http.put("/v1/memory/01J0000000000000000000000A", headers=admin, json=body)
    assert missing.status_code == 404
    empty = await api.http.put(
        "/v1/memory/01J0000000000000000000000A", headers=admin, json={"content": "", "version": 1}
    )
    assert empty.status_code == 422


async def test_an_item_under_a_legal_hold_cant_be_corrected(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    hit = await _found(api, admin)
    store = api.runtime.runtime.memory.store
    item = await store.get("acme", hit["id"])
    assert item is not None
    held = item.model_copy(update={"retention": "legal_hold", "version": item.version + 1})
    await store.replace(held, None, version=item.version)
    body = {"content": "Changed.", "version": held.version}
    assert (
        await api.http.put(f"/v1/memory/{hit['id']}", headers=admin, json=body)
    ).status_code == 403


async def test_only_the_operator_corrects_global_memory(api: Api) -> None:
    """Every tenant reads it, so one tenant's admin can't rewrite it for all of them."""
    platform = await api.bearer("platform-admin", name="p")
    admin = await api.bearer("tenant-admin", name="a")
    assert (await api.ingest(platform, scope="global")).status_code == 200
    search = {"query": "refunds", "scopes": ["global"]}
    [hit] = (await api.http.post("/v1/memory/search", headers=admin, json=search)).json()
    body = {"content": "Refunds never arrive.", "version": hit["version"]}
    path = f"/v1/memory/{hit['id']}"
    assert (await api.http.put(path, headers=admin, json=body)).status_code == 403
    assert (await api.http.put(path, headers=platform, json=body)).status_code == 200


async def test_another_tenants_item_cant_be_corrected(api: Api) -> None:
    hit = await _found(api, await api.bearer("tenant-admin"))
    theirs = await api.bearer("tenant-admin", tenant="globex", name="g")
    body = {"content": "Mine now.", "version": hit["version"]}
    assert (
        await api.http.put(f"/v1/memory/{hit['id']}", headers=theirs, json=body)
    ).status_code == 404


async def test_an_unknown_item_is_not_found(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    missing = await api.http.get("/v1/memory/01J0000000000000000000000A", headers=admin)
    assert missing.status_code == 404


async def test_erasure_removes_an_item_and_reports_it(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    await api.ingest(admin)
    [hit] = (
        await api.http.post("/v1/memory/search", headers=admin, json={"query": "refunds"})
    ).json()
    erased = await api.http.post("/v1/memory/forget", headers=admin, json={"item": hit["id"]})
    assert erased.json() == {"erased": 1, "held": 0}
    assert (await api.http.get(f"/v1/memory/{hit['id']}", headers=admin)).status_code == 404


@pytest.mark.parametrize(
    "body",
    [{}, {"item": "x", "subject": "user:alice@acme"}, {"subject": "not a principal"}],
    ids=["nothing", "both", "bad-subject"],
)
async def test_forget_needs_one_valid_target(api: Api, body: dict[str, str]) -> None:
    admin = await api.bearer("tenant-admin")
    assert (await api.http.post("/v1/memory/forget", headers=admin, json=body)).status_code == 422


@pytest.mark.parametrize("role", ["member", "auditor", "operator"])
async def test_memory_administration_is_for_admins(api: Api, role: str) -> None:
    headers = await api.bearer(role)
    assert (await api.ingest(headers)).status_code == 403
    search = await api.http.post("/v1/memory/search", headers=headers, json={"query": "x"})
    assert search.status_code == 403
    forget = await api.http.post(
        "/v1/memory/forget", headers=headers, json={"subject": "user:alice@acme"}
    )
    assert forget.status_code == 403


async def test_a_search_is_recorded_without_its_query(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    await api.ingest(admin)
    await api.http.post("/v1/memory/search", headers=admin, json={"query": "refunds"})
    events = await api.http.get("/v1/events", headers=admin, params={"pattern": "memory.recalled"})
    [recorded] = events.json()
    assert recorded["principal"] == "service:k@acme"
    assert "refunds" not in str(recorded)


async def test_only_a_platform_admin_can_write_global_memory(api: Api) -> None:
    """Every tenant reads it, so one tenant writing it would reach all the others."""
    tenant = await api.bearer("tenant-admin", name="admin")
    platform = await api.bearer("platform-admin", name="root")
    assert (await api.ingest(tenant, scope="global")).status_code == 403
    assert (await api.ingest(platform, scope="global")).status_code == 200


async def test_a_document_outside_its_source_is_refused(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    assert (await api.ingest(admin, source="other/")).status_code == 422


@pytest.mark.parametrize("scope", ["session", "user", "working", "agent"])
async def test_documents_can_not_be_ingested_into_private_scopes(api: Api, scope: str) -> None:
    admin = await api.bearer("tenant-admin")
    assert (await api.ingest(admin, scope=scope)).status_code == 422


async def test_ingesting_for_an_unknown_app_is_refused(api: Api) -> None:
    admin = await api.bearer("tenant-admin")
    response = await api.http.put(
        "/v1/apps/nope/knowledge", headers=admin, json={"source": "kb/", "documents": []}
    )
    assert response.status_code == 404


async def test_a_uri_is_only_a_label_and_never_opens_a_file(api: Api, tmp_path: Path) -> None:
    """Ingestion can't be aimed at the server's own disk: the text comes from the request."""
    secret = tmp_path / "secrets.txt"
    secret.write_text("the-server-password", "utf-8")
    admin = await api.bearer("tenant-admin")
    document = {"uri": str(secret), "text": "Harmless words from the request."}
    response = await api.http.put(
        KNOWLEDGE, headers=admin, json={"source": str(tmp_path), "documents": [document]}
    )
    assert response.status_code == 200
    search = {"query": "server password harmless words"}
    hits = (await api.http.post("/v1/memory/search", headers=admin, json=search)).json()
    assert [hit["content"] for hit in hits] == ["Harmless words from the request."]


# --------------------------------------------------------------------------- URLs


class Fetcher:
    name = "http"

    def __init__(self, result: Any) -> None:
        self.result = result
        self.urls: list[str] = []

    async def call(self, name: str, arguments: dict[str, Any], **_: Any) -> Any:
        self.urls.append(arguments["url"])
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _use(api: Api, fetcher: Fetcher) -> None:
    api.runtime.runtime._tool_providers = [fetcher]  # type: ignore[list-item]


async def test_a_page_can_be_ingested_by_url(api: Api) -> None:
    fetcher = Fetcher({"text": "Refunds take five days."})
    _use(api, fetcher)
    admin = await api.bearer("tenant-admin")
    report = await api.http.post(
        f"{KNOWLEDGE}/url", headers=admin, json={"url": "https://docs.acme.test/refunds"}
    )
    assert report.status_code == 200
    assert report.json()["added"] == 1
    [hit] = (
        await api.http.post("/v1/memory/search", headers=admin, json={"query": "refunds"})
    ).json()
    assert hit["provenance"]["source_uri"] == "https://docs.acme.test/refunds"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ToolDeniedError("internal.acme.test is not in the allowlist"), 403),
        (ToolInputError("only http(s) URLs can be fetched"), 422),
        (ToolError("fetch failed"), 502),
    ],
)
async def test_a_refused_url_stores_nothing(api: Api, error: Exception, status: int) -> None:
    _use(api, Fetcher(error))
    admin = await api.bearer("tenant-admin")
    response = await api.http.post(
        f"{KNOWLEDGE}/url", headers=admin, json={"url": "http://169.254.169.254/latest/meta-data"}
    )
    assert response.status_code == status
    found = await api.http.post("/v1/memory/search", headers=admin, json={"query": "meta data"})
    assert found.json() == []


async def test_the_real_fetcher_refuses_internal_addresses(api: Api) -> None:
    """Without an allowlist nothing can be fetched, so ingestion can't reach inside a network."""
    admin = await api.bearer("tenant-admin")
    for url in ("http://169.254.169.254/latest/meta-data", "http://localhost:8080/admin"):
        response = await api.http.post(f"{KNOWLEDGE}/url", headers=admin, json={"url": url})
        assert response.status_code == 403


async def test_a_session_joins_a_conversation(api: Api) -> None:
    member = await api.bearer("member")
    session = "01J0000000000000000000000B"
    started = await api.http.post(
        f"/v1/apps/{NAME}/runs",
        headers=member,
        json={"message": "hello", "session": session},
    )
    events = await api.runtime.runtime.events(NAME, started.json()["run_id"])
    assert events[0].correlationid == session
    assert events[0].runid != session


async def test_a_bad_session_id_is_refused(api: Api) -> None:
    member = await api.bearer("member")
    refused = await api.http.post(
        f"/v1/apps/{NAME}/runs", headers=member, json={"message": "x", "session": "not-a-ulid"}
    )
    assert refused.status_code == 422

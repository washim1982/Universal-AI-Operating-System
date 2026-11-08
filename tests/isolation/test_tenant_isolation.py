"""The isolation suite (M2 exit criterion 1, FR-SEC-06): two tenants on one installation.

Everything goes through the API with real credentials, on the in-memory fabric and, when
``AIOS_TEST_DATABASE_URL`` is set, on PostgreSQL with row-level security. ``globex`` tries to
reach ``acme``'s data by every route the API offers.
"""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.api import create_app
from aios.core.human import Approval
from aios.core.identity import Identity, Suspendable
from aios.core.ids import new_ulid
from aios.observability import EventMetrics
from aios.security.identity import ApiKeys
from aios.testing.human import request
from aios.testing.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps" / "ai-researcher"
CASSETTES = ROOT / "tests" / "cassettes" / "ai-researcher"
QUESTION = "What changed in the OpenTelemetry GenAI semantic conventions in 2026?"
NAME = "ai-researcher"
OPERATOR = "system:platform"


@dataclass
class World:
    runtime: LocalRuntime
    http: httpx2.AsyncClient
    acme: dict[str, str]
    globex: dict[str, str]
    run: str
    """A finished run of acme's."""


async def _bearer(runtime: LocalRuntime, tenant: str) -> dict[str, str]:
    _, value = await runtime.runtime.create_key(
        Identity(OPERATOR, tenant), "admin", ["tenant-admin"]
    )
    return {"Authorization": f"Bearer {value}"}


@pytest.fixture(params=["memory", "postgres"])
async def world(request: pytest.FixtureRequest) -> AsyncIterator[World]:
    durable = request.param == "postgres"
    if durable and not os.environ.get("AIOS_TEST_DATABASE_URL"):
        pytest.skip("set AIOS_TEST_DATABASE_URL to run")
    runtime = LocalRuntime.from_app(
        APP, cassettes=CASSETTES, tenants=["acme", "globex"], durable=durable
    )
    async with runtime:
        metrics = EventMetrics(runtime.bus)
        await metrics.start()
        app = create_app(
            runtime.runtime, metrics=metrics, authenticator=ApiKeys(runtime.runtime.security)
        )
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
            handle = await runtime.chat(NAME, QUESTION, tenant="acme")
            await handle.wait(timeout=60)
            yield World(
                runtime,
                http,
                await _bearer(runtime, "acme"),
                await _bearer(runtime, "globex"),
                handle.run_id,
            )
        await metrics.stop()


async def _get(world: World, path: str, headers: dict[str, str], **params: Any) -> Any:
    response = await world.http.get(path, headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_owner_can_read_its_own_run(world: World) -> None:
    """Without this, every "can't see it" below could pass on a broken API."""
    run = await _get(world, f"/v1/apps/{NAME}/runs/{world.run}", world.acme)
    assert run["status"] == "completed"
    assert await _get(world, "/v1/events", world.acme)


@pytest.mark.parametrize(
    "path",
    [
        "/v1/apps/ai-researcher/runs/{run}",
        "/v1/apps/ai-researcher/runs/{run}/events",
    ],
)
async def test_another_tenant_can_not_read_a_run(world: World, path: str) -> None:
    response = await world.http.get(path.format(run=world.run), headers=world.globex)
    assert response.status_code == 404


async def test_another_tenant_can_not_stream_a_run(world: World) -> None:
    response = await world.http.get(
        f"/v1/apps/{NAME}/runs/{world.run}/events",
        headers={**world.globex, "Accept": "text/event-stream"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("action", ["cancel", "pause", "resume"])
async def test_another_tenant_can_not_control_a_run(world: World, action: str) -> None:
    response = await world.http.post(
        f"/v1/apps/{NAME}/runs/{world.run}/{action}", headers=world.globex
    )
    assert response.status_code == 404


async def test_another_tenant_can_not_signal_a_run(world: World) -> None:
    response = await world.http.post(
        f"/v1/apps/{NAME}/runs/{world.run}/signal", headers=world.globex, json={"name": "go"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"app": NAME},
        {"app": "*"},
        {"pattern": ">"},
        {"pattern": "task.>"},
        {"after": 0, "limit": 5000},  # the most the API gives at once
        {"newest": "true", "limit": 5000},
    ],
)
async def test_the_event_log_shows_only_your_tenant(world: World, params: dict[str, Any]) -> None:
    mine = await world.runtime.chat(NAME, QUESTION, tenant="globex")
    await mine.wait(timeout=60)
    found = await _get(world, "/v1/events", world.globex, **params)
    assert {event["tenantid"] for event in found} == {"globex"}
    assert world.run not in {event.get("runid") for event in found}


@pytest.mark.parametrize("tenant", ["*", "acme", "ACME", "acme.*", ">"])
async def test_a_tenant_header_can_not_widen_or_change_the_tenant(
    world: World, tenant: str
) -> None:
    response = await world.http.get("/v1/events", headers={**world.globex, "X-AIOS-Tenant": tenant})
    assert response.status_code == 403


async def test_no_credential_means_nothing(world: World) -> None:
    for headers in ({}, {"Authorization": "Bearer nonsense"}, {"X-AIOS-Tenant": "acme"}):
        response = await world.http.get("/v1/events", headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_the_audit_log_shows_only_your_tenant(world: World) -> None:
    await world.runtime.runtime.create_key(Identity(OPERATOR, "globex"), "ci", ["member"])

    async def settled(tenant: str) -> list[Any]:
        return await world.runtime.runtime.audit.entries(tenant)

    await _eventually(lambda: settled("acme"))
    await _eventually(lambda: settled("globex"))
    for entry in await _get(world, "/v1/audit", world.globex):
        assert entry["tenantid"] == "globex"
    report = await _get(world, "/v1/audit/verify", world.globex)
    theirs = await _get(world, "/v1/audit/verify", world.acme)
    assert report["ok"]
    assert theirs["ok"]
    assert report["head"] != theirs["head"]


async def test_keys_belong_to_a_tenant(world: World) -> None:
    security = world.runtime.runtime.security
    theirs = (await security.keys("acme"))[0]
    listed = await _get(world, "/v1/keys", world.globex)
    assert theirs.id not in {key["id"] for key in listed}
    revoked = await world.http.delete(f"/v1/keys/{theirs.id}", headers=world.globex)
    assert revoked.status_code == 404
    assert (await security.key(theirs.id)).revoked_at is None  # type: ignore[union-attr]


async def test_a_key_never_shows_its_digest(world: World) -> None:
    created = await world.http.post(
        "/v1/keys", headers=world.acme, json={"name": "ci", "roles": ["tenant-admin"]}
    )
    listed = await _get(world, "/v1/keys", world.acme)
    assert "digest" not in created.json()
    assert all("digest" not in key for key in listed)


async def test_a_suspension_stays_inside_its_tenant(world: World) -> None:
    await world.runtime.runtime.suspend(
        Identity(OPERATOR, "acme"), Suspendable.APP, NAME, reason="incident"
    )
    assert await _get(world, "/v1/suspensions", world.globex) == []
    lifted = await world.http.delete(f"/v1/suspensions/app/{NAME}", headers=world.globex)
    assert lifted.status_code == 404
    started = await world.http.post(
        f"/v1/apps/{NAME}/runs", headers=world.globex, json={"message": QUESTION}
    )
    refused = await world.http.post(
        f"/v1/apps/{NAME}/runs", headers=world.acme, json={"message": QUESTION}
    )
    assert started.status_code == 202
    assert refused.status_code == 403


async def _ingest(world: World, headers: dict[str, str], text: str) -> None:
    documents = [{"uri": "kb/a.md", "text": text}]
    body = {"source": "kb/", "documents": documents}
    response = await world.http.put(f"/v1/apps/{NAME}/knowledge", headers=headers, json=body)
    assert response.status_code == 200, response.text


async def _memory(world: World, headers: dict[str, str], query: str) -> list[dict[str, Any]]:
    body = {"query": query, "scopes": ["app", "tenant", "user"]}
    response = await world.http.post("/v1/memory/search", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_memory_search_never_crosses_tenants(world: World) -> None:
    """FR-MEM-06. Both tenants have the same App, the same document path and the same words."""
    await _ingest(world, world.acme, "Order 1003 was refunded for acme.")
    await _ingest(world, world.globex, "Order 1003 was refunded for globex.")
    assert [hit["content"] for hit in await _memory(world, world.acme, "order 1003")] == [
        "Order 1003 was refunded for acme."
    ]
    assert [hit["content"] for hit in await _memory(world, world.globex, "order 1003")] == [
        "Order 1003 was refunded for globex."
    ]


async def test_another_tenants_memory_can_not_be_read_or_erased_by_id(world: World) -> None:
    await _ingest(world, world.acme, "Order 1003 was refunded.")
    [hit] = await _memory(world, world.acme, "order 1003")
    item = hit["id"]
    assert (await world.http.get(f"/v1/memory/{item}", headers=world.globex)).status_code == 404
    erased = await world.http.post("/v1/memory/forget", headers=world.globex, json={"item": item})
    assert erased.json() == {"erased": 0, "held": 0}
    assert (await world.http.get(f"/v1/memory/{item}", headers=world.acme)).status_code == 200


async def test_forgetting_a_subject_stays_in_its_tenant(world: World) -> None:
    await _ingest(world, world.acme, "Refund notes.")
    erased = await world.http.post(
        "/v1/memory/forget", headers=world.globex, json={"subject": "user:alice@acme"}
    )
    assert erased.json() == {"erased": 0, "held": 0}
    assert await _memory(world, world.acme, "refund notes")


async def test_one_tenants_resync_can_not_prune_anothers_documents(world: World) -> None:
    await _ingest(world, world.acme, "Refund notes.")
    body: dict[str, Any] = {"source": "kb/", "documents": []}
    await world.http.put(f"/v1/apps/{NAME}/knowledge", headers=world.globex, json=body)
    assert await _memory(world, world.acme, "refund notes")


async def test_metrics_name_every_tenant_so_tenants_can_not_read_them(world: World) -> None:
    response = await world.http.get("/metrics", headers=world.globex)
    assert response.status_code == 403


async def _eventually(check: Any, seconds: float = 5.0) -> Any:
    import asyncio  # noqa: PLC0415

    async with asyncio.timeout(seconds):
        while not (value := await check()):  # noqa: ASYNC110 - no event to wait on
            await asyncio.sleep(0.02)
    return value


# --------------------------------------------------------------------------- approvals


async def _acme_approval(world: World) -> Approval:
    # Its own run, which hasn't ended: the Human OS cancels what an ended run was waiting for,
    # and world.run has ended.
    approval = Approval.of(
        request(run=new_ulid(), expires_at=datetime.now(UTC) + timedelta(hours=1))
    )
    await world.runtime.runtime.human.store.add(approval)
    return approval


async def test_the_owner_sees_its_own_approval(world: World) -> None:
    """Without this, the refusals below could pass on a broken route."""
    approval = await _acme_approval(world)
    found = await _get(world, f"/v1/approvals/{approval.id}", world.acme)
    assert found["approval"] == approval.id


async def test_another_tenant_can_not_read_an_approval(world: World) -> None:
    approval = await _acme_approval(world)
    response = await world.http.get(f"/v1/approvals/{approval.id}", headers=world.globex)
    assert response.status_code == 404


async def test_another_tenant_can_not_list_an_approval(world: World) -> None:
    await _acme_approval(world)
    assert await _get(world, "/v1/approvals", world.globex, everyone=True) == []


@pytest.mark.parametrize("verdict", ["approve", "deny"])
async def test_another_tenant_can_not_answer_an_approval(world: World, verdict: str) -> None:
    approval = await _acme_approval(world)
    response = await world.http.post(f"/v1/approvals/{approval.id}/{verdict}", headers=world.globex)
    assert response.status_code == 404
    found = await world.runtime.runtime.human.get("acme", approval.id)
    assert found is not None
    assert found.status == "pending"

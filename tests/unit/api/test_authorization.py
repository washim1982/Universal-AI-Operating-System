"""The API boundary (ADR-011; FR-SEC-04): every route is gated, and roles mean what they say."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from aios.api import create_app
from aios.core.envelope import Event
from aios.core.identity import Identity, Suspendable
from aios.observability import EventMetrics
from aios.security.identity import ApiKeys
from aios.testing.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "apps" / "ai-researcher"
CASSETTES = ROOT / "tests" / "cassettes" / "ai-researcher"
NAME = "ai-researcher"
OPERATOR = "system:platform"
PUBLIC = {"/healthz"}
SIGNED = {"/v1/hooks/{tenant}/{app}/{name}"}  # answers to a signature, not a role
UNKNOWN = "01J0000000000000000000000A"
MANIFEST = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: waiter, version: 0.1.0}
spec:
  entrypoints: [{type: chat, workflow: wait}]
  workflows: [wait]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: wait, version: 0.1.0}
spec:
  steps:
    - {id: reply, type: wait, signal: reply}
"""


class Api:
    def __init__(self, http: httpx2.AsyncClient, runtime: LocalRuntime, app: FastAPI) -> None:
        self.http = http
        self.runtime = runtime
        self.app = app

    async def bearer(self, *roles: str, tenant: str = "acme", name: str = "k") -> dict[str, str]:
        _, value = await self.runtime.runtime.create_key(
            Identity(OPERATOR, tenant), name, list(roles)
        )
        return {"Authorization": f"Bearer {value}"}


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime:
        metrics = EventMetrics(runtime.bus)
        await metrics.start()
        app = create_app(
            runtime.runtime, metrics=metrics, authenticator=ApiKeys(runtime.runtime.security)
        )
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
            yield Api(http, runtime, app)
        await metrics.stop()


def _routes(app: FastAPI) -> list[tuple[str, str]]:
    return sorted(
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    )


def _filled(path: str) -> str:
    return (
        path.replace("{app}", NAME)
        .replace("{run_id}", UNKNOWN)
        .replace("{other}", UNKNOWN)
        .replace("{letter}", UNKNOWN)
        .replace("{key_id}", UNKNOWN)
        .replace("{item_id}", UNKNOWN)
        .replace("{kind}", "agent")
        .replace("{name}", "someone")
    )


async def test_no_route_answers_without_a_credential(api: Api) -> None:
    """A route added without a gate fails here, with no test written for it."""
    routes = _routes(api.app)
    assert len(routes) > 20
    for method, path in routes:
        if path in PUBLIC:
            continue
        response = await api.http.request(method, _filled(path))
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"


async def test_no_route_is_open_to_a_role_nobody_defined(api: Api) -> None:
    headers = await api.bearer("support-lead")
    for method, path in _routes(api.app):
        if path in PUBLIC | SIGNED:
            continue
        response = await api.http.request(method, _filled(path), headers=headers)
        assert response.status_code == 403, f"{method} {path} answered {response.status_code}"


async def test_health_needs_no_credential(api: Api) -> None:
    assert (await api.http.get("/healthz")).status_code == 200


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer ", "Basic abc", "aios_x_y", "bearer nonsense"],
)
async def test_malformed_authorization_is_refused(api: Api, header: str) -> None:
    response = await api.http.get("/v1/apps", headers={"Authorization": header})
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("role", "method", "path", "status"),
    [
        ("member", "GET", "/v1/apps", 200),
        ("member", "GET", "/v1/events", 403),
        ("member", "GET", "/v1/audit", 403),
        ("member", "GET", "/v1/keys", 403),
        ("member", "GET", "/metrics", 403),
        ("member", "POST", "/v1/apply", 403),
        ("auditor", "GET", "/v1/audit", 200),
        ("auditor", "GET", "/v1/audit/verify", 200),
        ("auditor", "GET", "/v1/events", 200),
        ("auditor", "GET", "/v1/keys", 403),
        ("auditor", "GET", "/v1/suspensions", 403),
        ("operator", "GET", "/v1/dlq", 200),
        ("operator", "GET", "/v1/audit", 403),
        ("tenant-admin", "GET", "/v1/keys", 200),
        ("tenant-admin", "GET", "/v1/suspensions", 200),
        ("tenant-admin", "GET", "/metrics", 403),
        ("tenant-admin", "POST", "/v1/apply", 403),
        ("tenant-admin", "POST", "/v1/installations", 403),
        ("operator", "GET", "/v1/installations", 403),
        ("member", "GET", "/v1/installations/none", 403),
        ("tenant-admin", "GET", "/v1/installations", 200),
        ("tenant-admin", "POST", "/v1/installations/none/refuse", 404),
        ("operator", "POST", "/v1/installations/none/consent", 403),
        ("auditor", "DELETE", "/v1/installations/none?data=retain", 403),
        ("platform-admin", "GET", "/metrics", 200),
        ("member", "GET", "/v1/agents", 200),
        ("member", "GET", "/v1/costs", 403),
        ("operator", "GET", "/v1/costs", 200),
        ("auditor", "GET", "/v1/costs", 200),
        ("member", "GET", f"/v1/apps/{NAME}/policies", 403),
        ("operator", "GET", f"/v1/apps/{NAME}/policies", 403),
        ("auditor", "GET", f"/v1/apps/{NAME}/policies", 200),
        ("tenant-admin", "GET", f"/v1/apps/{NAME}/policies", 200),
        ("member", "GET", f"/v1/apps/{NAME}/runs/{UNKNOWN}/trace", 404),
        ("operator", "PUT", f"/v1/memory/{UNKNOWN}", 403),
        ("member", "GET", "/v1/installations/none/versions", 403),
        ("tenant-admin", "GET", "/v1/installations/none/versions", 404),
        ("member", "POST", f"/v1/apps/{NAME}/runs/{UNKNOWN}/replay", 404),
        ("auditor", "POST", f"/v1/apps/{NAME}/runs/{UNKNOWN}/replay", 403),
        ("member", "POST", f"/v1/apps/{NAME}/runs/{UNKNOWN}/retry", 403),
        ("operator", "POST", f"/v1/apps/{NAME}/runs/{UNKNOWN}/retry", 404),
        ("auditor", "GET", f"/v1/apps/{NAME}/runs/{UNKNOWN}/compare/{UNKNOWN}", 404),
    ],
)
async def test_roles_over_http(api: Api, role: str, method: str, path: str, status: int) -> None:
    body = {"content": "x", "version": 1} if method == "PUT" else None
    response = await api.http.request(method, path, headers=await api.bearer(role), json=body)
    assert response.status_code == status


async def test_an_auditor_can_ask_the_simulator(api: Api) -> None:
    question = {"action": "model.route", "agent": "planner-agent"}
    path = f"/v1/apps/{NAME}/policies/simulate"
    member = await api.http.post(path, json=question, headers=await api.bearer("member"))
    auditor = await api.http.post(path, json=question, headers=await api.bearer("auditor"))
    assert (member.status_code, auditor.status_code) == (403, 200)


async def test_only_a_platform_admin_can_install_code(api: Api) -> None:
    content = {"Content-Type": "application/yaml"}
    admin = await api.bearer("tenant-admin", name="admin")
    platform = await api.bearer("platform-admin", name="platform")
    refused = await api.http.post("/v1/apply", content=MANIFEST, headers={**admin, **content})
    allowed = await api.http.post("/v1/apply", content=MANIFEST, headers={**platform, **content})
    assert refused.status_code == 403
    assert allowed.status_code == 201


async def test_a_denied_call_is_recorded(api: Api) -> None:
    headers = await api.bearer("member")
    assert (await api.http.get("/v1/audit", headers=headers)).status_code == 403
    audit = api.runtime.runtime.audit

    async def recorded() -> list[Event]:
        entries = await audit.entries("acme")
        return [e.event for e in entries if e.event.type == "policy.decision.denied"]

    async with asyncio.timeout(5):
        while not await recorded():  # noqa: ASYNC110 - no event to wait on
            await asyncio.sleep(0.01)
    [denied] = await recorded()
    assert denied.principal == "service:k@acme"
    assert isinstance(denied.data, dict)
    assert denied.data["action"] == "audit.read"


async def test_the_caller_is_on_the_run(api: Api) -> None:
    headers = await api.bearer("member", name="ci")
    started = await api.http.post(
        f"/v1/apps/{NAME}/runs", headers=headers, json={"message": "why?"}
    )
    events = await api.runtime.runtime.events(NAME, started.json()["run_id"])
    assert events[0].principal == "service:ci@acme"
    assert events[0].onbehalfof == "service:ci@acme"


# --------------------------------------------------------------------------- keys


async def test_a_key_is_shown_once_and_works(api: Api) -> None:
    headers = await api.bearer("tenant-admin", name="admin")
    created = (
        await api.http.post(
            "/v1/keys", headers=headers, json={"name": "ci", "roles": ["tenant-admin"]}
        )
    ).json()
    value = created.pop("key")
    assert created["principal"] == "service:ci@acme"
    listed = (await api.http.get("/v1/keys", headers=headers)).json()
    assert all(value not in str(key) for key in listed)
    fresh = {"Authorization": f"Bearer {value}"}
    assert (await api.http.get("/v1/apps", headers=fresh)).status_code == 200


@pytest.mark.parametrize("role", ["member", "auditor", "operator", "tenant-admin", "support-lead"])
async def test_a_tenant_admin_can_grant_the_roles_of_the_tenant(api: Api, role: str) -> None:
    headers = await api.bearer("tenant-admin", name="admin")
    created = await api.http.post("/v1/keys", headers=headers, json={"name": "ci", "roles": [role]})
    assert created.status_code == 201


async def test_only_a_platform_admin_can_grant_platform_admin(api: Api) -> None:
    body = {"name": "ci", "roles": ["member", "platform-admin"]}
    tenant = await api.bearer("tenant-admin", name="admin")
    platform = await api.bearer("platform-admin", name="platform")
    refused = await api.http.post("/v1/keys", headers=tenant, json=body)
    allowed = await api.http.post("/v1/keys", headers=platform, json=body)
    assert refused.status_code == 403
    assert "platform-admin" in refused.json()["detail"]
    assert allowed.status_code == 201


async def test_a_key_stops_working_the_moment_it_is_revoked(api: Api) -> None:
    admin = await api.bearer("tenant-admin", name="admin")
    created = (
        await api.http.post(
            "/v1/keys", headers=admin, json={"name": "ci", "roles": ["tenant-admin"]}
        )
    ).json()
    fresh = {"Authorization": f"Bearer {created['key']}"}
    assert (await api.http.get("/v1/apps", headers=fresh)).status_code == 200
    assert (await api.http.delete(f"/v1/keys/{created['id']}", headers=admin)).status_code == 204
    assert (await api.http.get("/v1/apps", headers=fresh)).status_code == 401
    assert (await api.http.delete(f"/v1/keys/{created['id']}", headers=admin)).status_code == 404


@pytest.mark.parametrize(
    "body", [{"name": "Bad Name", "roles": ["member"]}, {"name": "ci", "roles": []}]
)
async def test_a_bad_key_request_is_refused(api: Api, body: dict[str, Any]) -> None:
    headers = await api.bearer("tenant-admin", name="admin")
    assert (await api.http.post("/v1/keys", headers=headers, json=body)).status_code == 422


# --------------------------------------------------------------------------- the kill switch


async def test_a_suspended_app_refuses_new_runs_and_comes_back(api: Api) -> None:
    admin = await api.bearer("tenant-admin", name="admin")
    runs = f"/v1/apps/{NAME}/runs"
    body = {"kind": "app", "name": NAME, "reason": "incident 42"}
    assert (await api.http.post("/v1/suspensions", headers=admin, json=body)).status_code == 201
    refused = await api.http.post(runs, headers=admin, json={"message": "x"})
    assert refused.status_code == 403
    assert "incident 42" in refused.json()["detail"]
    listed = (await api.http.get("/v1/suspensions", headers=admin)).json()
    assert [(s["kind"], s["name"], s["by"]) for s in listed] == [
        ("app", NAME, "service:admin@acme")
    ]
    lifted = await api.http.delete(f"/v1/suspensions/app/{NAME}", headers=admin)
    assert lifted.status_code == 204
    assert (await api.http.post(runs, headers=admin, json={"message": "x"})).status_code == 202


async def test_a_suspended_tenant_can_not_sign_in_to_do_anything(api: Api) -> None:
    admin = await api.bearer("tenant-admin", name="admin")
    body = {"kind": "tenant", "name": "ignored", "reason": "breach"}
    done = await api.http.post("/v1/suspensions", headers=admin, json=body)
    assert done.json()["name"] == "acme"
    assert (await api.http.get("/v1/apps", headers=admin)).status_code == 403
    # Lifting it takes the operator: the tenant's own admin is locked out too.
    assert (await api.http.delete("/v1/suspensions/tenant/acme", headers=admin)).status_code == 403
    await api.runtime.runtime.lift_suspension(
        Identity(OPERATOR, "acme"), Suspendable.TENANT, "acme"
    )
    assert (await api.http.get("/v1/apps", headers=admin)).status_code == 200


async def test_a_bad_suspension_is_refused(api: Api) -> None:
    admin = await api.bearer("tenant-admin", name="admin")
    for body in (
        {"kind": "planet", "name": "x", "reason": "r"},
        {"kind": "app", "name": "x", "reason": ""},
    ):
        assert (await api.http.post("/v1/suspensions", headers=admin, json=body)).status_code == 422
    assert (await api.http.delete("/v1/suspensions/app/x", headers=admin)).status_code == 404


# --------------------------------------------------------------------------- local development


@pytest.fixture
async def local() -> AsyncIterator[httpx2.AsyncClient]:
    async with LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime:
        transport = httpx2.ASGITransport(app=create_app(runtime.runtime))
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
            yield http


async def test_a_local_server_needs_no_credential(local: httpx2.AsyncClient) -> None:
    assert (await local.get("/v1/apps")).status_code == 200


@pytest.mark.parametrize("tenant", ["*", "acme.*", ">", "Bad Tenant"])
async def test_even_a_local_server_refuses_a_wildcard_tenant(
    local: httpx2.AsyncClient, tenant: str
) -> None:
    """``*`` once read every tenant's events, because the fabric takes it as "all tenants"."""
    response = await local.get("/v1/events", headers={"X-AIOS-Tenant": tenant})
    assert response.status_code == 400


async def test_a_bad_event_pattern_is_a_client_error(local: httpx2.AsyncClient) -> None:
    assert (await local.get("/v1/events", params={"pattern": "a..b"})).status_code == 422
    assert (await local.get("/v1/events", params={"app": "Not An App"})).status_code == 422

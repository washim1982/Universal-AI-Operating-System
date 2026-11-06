"""The installation routes (ADR-019): offer, consent, configure, roll back, export, uninstall."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from aios.api import create_app
from aios.controlplane.bundle import bundle_from_documents
from aios.testing.runtime import LocalRuntime

APP = "waiter"
DOCUMENTS = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: waiter, version: %s}
spec:
  entrypoints: [{type: chat, workflow: wait}]
  workflows: [wait]
  permissions: {tools: [%s]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: wait, version: %s}
spec:
  steps:
    - {id: reply, type: wait, signal: reply}
"""


def documents(version: str = "0.1.0", tools: str = "mail.message.read") -> str:
    return DOCUMENTS % (version, tools, version)


def bundle(version: str = "0.1.0", tools: str = "mail.message.read") -> dict[str, Any]:
    return bundle_from_documents(documents(version, tools)).model_dump(mode="json")


@pytest.fixture
async def api() -> AsyncIterator[httpx2.AsyncClient]:
    async with LocalRuntime() as runtime:
        transport = httpx2.ASGITransport(app=create_app(runtime.runtime))
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as client:
            yield client


async def apply(api: httpx2.AsyncClient, consent: str | None = None, **values: str) -> Any:
    params = {"consent": consent} if consent else {}
    answer = await api.post(
        "/v1/apply",
        params=params,
        content=documents(**values),
        headers={"Content-Type": "application/yaml"},
    )
    assert answer.status_code == 201, answer.text
    return answer.json()


async def test_apply_asks_unless_told_otherwise(api: httpx2.AsyncClient) -> None:
    found = await apply(api)
    assert (found["status"], found["version"], found["pending"]) == (
        "awaiting-consent",
        None,
        "0.1.0",
    )
    assert found["requested"]["tools"] == ["mail.message.read"]
    assert (await api.post(f"/v1/apps/{APP}/runs", json={"message": "x"})).status_code == 404


async def test_apply_with_consent_all_runs_it(api: httpx2.AsyncClient) -> None:
    found = await apply(api, "all")
    assert (found["status"], found["version"]) == ("active", "0.1.0")
    assert "requested" not in found
    assert (await api.post(f"/v1/apps/{APP}/runs", json={"message": "x"})).status_code == 202


async def test_what_is_asked_is_shown_until_answered(api: httpx2.AsyncClient) -> None:
    await apply(api)
    shown = (await api.get(f"/v1/installations/{APP}")).json()
    assert shown["requested"]["tools"] == ["mail.message.read"]
    [listed] = (await api.get("/v1/installations")).json()
    assert listed["requested"] == shown["requested"]
    agreed = await api.post(f"/v1/installations/{APP}/consent", json={})
    assert agreed.status_code == 200
    assert "requested" not in agreed.json()


async def test_consent_can_leave_things_out(api: httpx2.AsyncClient) -> None:
    await apply(api)
    agreed = await api.post(f"/v1/installations/{APP}/consent", json={"grants": {"tools": []}})
    assert agreed.json()["grants"]["tools"] == []
    assert agreed.json()["refused"]["tools"] == ["mail.message.read"]


async def test_nobody_grants_what_wasnt_asked_for(api: httpx2.AsyncClient) -> None:
    await apply(api)
    grants = {"tools": ["pay.refund.create"]}
    answer = await api.post(f"/v1/installations/{APP}/consent", json={"grants": grants})
    assert answer.status_code == 422


async def test_answering_twice_is_a_conflict(api: httpx2.AsyncClient) -> None:
    await apply(api, "all")
    assert (await api.post(f"/v1/installations/{APP}/consent", json={})).status_code == 409
    assert (await api.post(f"/v1/installations/{APP}/refuse")).status_code == 409


async def test_an_unknown_app_is_not_found(api: httpx2.AsyncClient) -> None:
    for method, path in [
        ("GET", "/v1/installations/nope"),
        ("POST", "/v1/installations/nope/consent"),
        ("POST", "/v1/installations/nope/refuse"),
        ("PUT", "/v1/installations/nope/config"),
        ("DELETE", "/v1/installations/nope?data=retain"),
    ]:
        answer = await api.request(method, path, json={} if method in ("POST", "PUT") else None)
        assert answer.status_code == 404, (method, path, answer.text)


async def test_a_bundle_can_be_offered_whole(api: httpx2.AsyncClient) -> None:
    answer = await api.post("/v1/installations", json={"bundle": bundle()})
    assert answer.status_code == 201, answer.text
    assert answer.json()["pending"] == "0.1.0"


async def test_a_forged_bundle_is_refused(api: httpx2.AsyncClient) -> None:
    forged = bundle() | {"documents": documents("0.1.0", "pay.refund.create")}
    answer = await api.post("/v1/installations", json={"bundle": forged})
    assert answer.status_code == 422
    assert "digest" in answer.text


async def test_a_version_never_changes(api: httpx2.AsyncClient) -> None:
    await api.post("/v1/installations", json={"bundle": bundle()})
    other = await api.post("/v1/installations", json={"bundle": bundle(tools="mail.message.send")})
    assert other.status_code == 409


async def test_rollback_to_a_version_never_had(api: httpx2.AsyncClient) -> None:
    await apply(api, "all")
    answer = await api.post(f"/v1/installations/{APP}/rollback", json={"version": "9.9.9"})
    assert answer.status_code == 404


async def test_rollback_to_an_earlier_version(api: httpx2.AsyncClient) -> None:
    await apply(api, "all")
    await apply(api, "all", version="0.2.0")
    back = await api.post(f"/v1/installations/{APP}/rollback", json={"version": "0.1.0"})
    assert (back.status_code, back.json()["version"]) == (200, "0.1.0")


async def test_every_version_is_listed_newest_first(api: httpx2.AsyncClient) -> None:
    await apply(api, "all")
    await apply(api, "all", version="0.2.0", tools="mail.message.send")
    versions = (await api.get(f"/v1/installations/{APP}/versions")).json()
    assert [v["version"] for v in versions] == ["0.2.0", "0.1.0"]
    assert [v["running"] for v in versions] == [True, False]
    assert versions[0]["requested"]["tools"] == ["mail.message.send"]
    assert versions[1]["digest"].startswith("sha256:")


async def test_uninstall_says_what_happens_to_data(api: httpx2.AsyncClient) -> None:
    await apply(api, "all")
    assert (await api.delete(f"/v1/installations/{APP}")).status_code == 422
    assert (await api.delete(f"/v1/installations/{APP}?data=keep")).status_code == 422
    gone = await api.delete(f"/v1/installations/{APP}?data=delete")
    assert (gone.status_code, gone.json()["status"]) == (200, "uninstalled")
    assert (await api.post(f"/v1/apps/{APP}/runs", json={"message": "x"})).status_code == 404


async def test_exporting_an_app_that_isnt_there(api: httpx2.AsyncClient) -> None:
    assert (await api.get("/v1/installations/nope/export")).status_code == 404


async def test_export_is_a_line_of_json_for_each_thing(api: httpx2.AsyncClient) -> None:
    await apply(api, "all")
    await api.post(f"/v1/apps/{APP}/runs", json={"message": "x"})
    answer = await api.get(f"/v1/installations/{APP}/export")
    assert answer.status_code == 200
    lines = [json.loads(line) for line in answer.text.splitlines()]
    assert any(line.get("event", {}).get("type") == "app.request.received" for line in lines)

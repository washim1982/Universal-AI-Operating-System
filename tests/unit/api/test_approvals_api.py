"""Approvals through the API: the inbox, the answer and who may give it (FR-HUM-03, 04, 07)."""

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import httpx2
import pytest

from aios.api import create_app
from aios.core.identity import Identity, Suspendable
from aios.core.manifests import load_manifests
from aios.core.policy import SideEffect
from aios.fabric import Scope
from aios.kernel import Context, agent
from aios.observability import EventMetrics
from aios.security.identity import ApiKeys
from aios.testing.runtime import LocalRuntime
from aios.tools import NativeProvider, tool

OPERATOR = "system:platform"
UNKNOWN = "01J0000000000000000000000A"
MANIFESTS = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 0.1.0}
spec:
  entrypoints: [{type: chat, workflow: ticket}]
  workflows: [ticket]
  agents: [refund-agent]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: refund-agent, version: 0.1.0}
spec:
  runtime: {type: python, entrypoint: "desk_api_handlers:handler"}
  capabilities: [{name: refunds.issue}]
  tools: {allow: ["pay.refund.create"]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: ticket, version: 0.1.0}
spec:
  steps:
    - {id: refund, type: task, capability: refunds.issue}
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: refunds, version: 1.0.0}
spec:
  subject: {app: desk}
  requireApproval:
    - {action: tool.invoke, resource: pay.refund.create, approvers: ["role:lead"]}
"""


@agent("refund-agent", capability="refunds.issue")
async def refund(ctx: Context, task: Any) -> str:
    await ctx.tools.invoke("pay.refund.create", amountUSD=120, rationale="it arrived broken")
    return "refunded"


@dataclass
class Api:
    http: httpx2.AsyncClient
    runtime: LocalRuntime
    metrics: EventMetrics
    refunds: list[float]

    async def bearer(self, name: str, *roles: str, tenant: str = "acme") -> dict[str, str]:
        _, value = await self.runtime.runtime.create_key(
            Identity(OPERATOR, tenant), name, list(roles)
        )
        return {"Authorization": f"Bearer {value}"}

    async def asked(self, headers: dict[str, str]) -> tuple[str, str]:
        """Start a run as this caller, and wait for its approval. Returns the run and approval."""
        response = await self.http.post(
            "/v1/apps/desk/runs", headers=headers, json={"message": "x"}
        )
        assert response.status_code == 202, response.text
        run = response.json()["run_id"]
        async with asyncio.timeout(10):
            while True:
                async for stored in self.runtime.bus.replay(
                    Scope("acme", "desk"), "approval.requested", run_id=run
                ):
                    data = stored.event.data
                    assert isinstance(data, dict)
                    approval = str(data["approval"])
                    if await self.runtime.runtime.human.get("acme", approval):
                        return run, approval
                await asyncio.sleep(0.01)

    async def status(self, run: str) -> dict[str, Any]:
        state = await self.runtime.runtime.run_state("desk", run, tenant="acme")
        return {"status": state.status, "waiting": state.waiting}


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    module = ModuleType("desk_api_handlers")
    module.handler = refund  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "desk_api_handlers", module)
    refunds: list[float] = []

    @tool("pay.refund.create", side_effect=SideEffect.FINANCIAL)
    async def create(amountUSD: float) -> dict[str, str]:
        refunds.append(amountUSD)
        return {"status": "refunded"}

    async with LocalRuntime(tenants=["acme", "globex"]) as runtime:
        for tenant in ("acme", "globex"):
            await runtime.runtime.apply(load_manifests(MANIFESTS), tenant=tenant)
        await runtime.runtime.register_tool_provider(NativeProvider("pay", [create]))
        metrics = EventMetrics(runtime.bus)
        await metrics.start()
        app = create_app(
            runtime.runtime, metrics=metrics, authenticator=ApiKeys(runtime.runtime.security)
        )
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
            yield Api(http, runtime, metrics, refunds)
        await metrics.stop()


async def _approve(
    api: Api, approval: str, headers: dict[str, str], **body: Any
) -> httpx2.Response:
    return await api.http.post(f"/v1/approvals/{approval}/approve", headers=headers, json=body)


async def _finished(api: Api, run: str) -> str:
    async with asyncio.timeout(10):
        while True:
            status = (await api.status(run))["status"]
            if status in ("completed", "failed", "cancelled"):
                return status
            await asyncio.sleep(0.01)


async def test_the_approver_sees_it_answers_it_and_the_refund_happens(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    run, approval = await api.asked(alice)

    inbox = (await api.http.get("/v1/approvals", headers=lead)).json()
    assert [entry["approval"] for entry in inbox] == [approval]
    entry = inbox[0]
    assert entry["payload"] == {"amountUSD": 120}
    assert entry["rationale"] == "it arrived broken"
    assert entry["risk"] == "high"
    assert entry["policies"] == ["refunds/approval/0"]
    assert entry["trace"] == f"/v1/apps/desk/runs/{run}/events"
    assert entry["status"] == "pending"
    assert entry["initiator"].startswith("service:alice")

    answered = await _approve(api, approval, lead, comment="fine by me")
    assert answered.status_code == 200, answered.text
    assert answered.json()["status"] == "granted"
    assert answered.json()["comment"] == "fine by me"
    assert await _finished(api, run) == "completed"
    assert api.refunds == [120]


async def test_a_denial_stops_the_refund(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    run, approval = await api.asked(alice)
    denied = await api.http.post(f"/v1/approvals/{approval}/deny", headers=lead, json={})
    assert denied.status_code == 200
    assert denied.json()["status"] == "denied"
    assert await _finished(api, run) == "failed"
    assert api.refunds == []


async def test_an_answer_needs_no_body(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    _, approval = await api.asked(alice)
    response = await api.http.post(f"/v1/approvals/{approval}/approve", headers=lead)
    assert response.status_code == 200, response.text


async def test_whoever_started_it_cant_approve_it_and_the_attempt_is_on_record(api: Api) -> None:
    both = await api.bearer("sam", "member", "lead")
    run, approval = await api.asked(both)
    refused = await _approve(api, approval, both)
    assert refused.status_code == 403
    assert "whoever" in refused.json()["detail"]
    assert (await api.http.get("/v1/approvals", headers=both)).json() == []
    assert (await api.status(run))["status"] == "paused"
    denied = [
        s.event.data async for s in api.runtime.bus.replay(Scope("acme"), "policy.decision.denied")
    ]
    assert any(isinstance(d, dict) and d["resource"] == approval for d in denied)


async def test_someone_the_rule_doesnt_name_cant_answer(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    admin = await api.bearer("ada", "tenant-admin")
    _, approval = await api.asked(alice)
    assert (await _approve(api, approval, admin)).status_code == 403
    assert (await api.http.get("/v1/approvals", headers=admin)).json() == []


async def test_a_role_alone_gets_nothing_without_a_built_in_one(api: Api) -> None:
    """Custom roles grant nothing (ADR-011): an approver also needs ``member``."""
    alice = await api.bearer("alice", "member")
    only = await api.bearer("lead", "lead")
    _, approval = await api.asked(alice)
    assert (await _approve(api, approval, only)).status_code == 403
    assert (await api.http.get("/v1/approvals", headers=only)).status_code == 403


async def test_an_auditor_can_read_but_not_answer(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    auditor = await api.bearer("audrey", "auditor")
    _, approval = await api.asked(alice)
    listed = await api.http.get("/v1/approvals", headers=auditor, params={"everyone": True})
    assert [entry["approval"] for entry in listed.json()] == [approval]
    assert (await api.http.get(f"/v1/approvals/{approval}", headers=auditor)).status_code == 200
    assert (await _approve(api, approval, auditor)).status_code == 403


async def test_a_read_only_role_cant_answer_even_if_a_rule_names_it(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    auditor = await api.bearer("audrey", "auditor", "lead")
    _, approval = await api.asked(alice)
    assert (await _approve(api, approval, auditor)).status_code == 403


async def test_a_member_cant_list_everyones(api: Api) -> None:
    member = await api.bearer("alice", "member")
    response = await api.http.get("/v1/approvals", headers=member, params={"everyone": True})
    assert response.status_code == 403


async def test_the_payload_stays_with_those_it_is_for(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    bystander = await api.bearer("bob", "member")
    _, approval = await api.asked(alice)
    assert (await api.http.get(f"/v1/approvals/{approval}", headers=bystander)).status_code == 404


async def test_a_closed_approval_answers_409(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    _, approval = await api.asked(alice)
    assert (await _approve(api, approval, lead)).status_code == 200
    again = await _approve(api, approval, lead)
    assert again.status_code == 409
    assert "already granted" in again.json()["detail"]


async def test_an_unknown_approval_answers_404(api: Api) -> None:
    lead = await api.bearer("lead", "member", "lead")
    assert (await api.http.get(f"/v1/approvals/{UNKNOWN}", headers=lead)).status_code == 404
    assert (await _approve(api, UNKNOWN, lead)).status_code == 404


async def test_a_closed_list_is_for_auditors(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    auditor = await api.bearer("audrey", "auditor")
    _, approval = await api.asked(alice)
    await _approve(api, approval, lead)
    params = {"status": "granted", "everyone": True}
    found = (await api.http.get("/v1/approvals", headers=auditor, params=params)).json()
    assert [entry["approval"] for entry in found] == [approval]
    only_mine = await api.http.get("/v1/approvals", headers=lead, params={"status": "granted"})
    assert only_mine.status_code == 422


async def test_a_suspended_app_cant_have_its_approvals_answered(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    _, approval = await api.asked(alice)
    await api.runtime.runtime.suspend(
        Identity(OPERATOR, "acme"), Suspendable.APP, "desk", reason="incident"
    )
    refused = await _approve(api, approval, lead)
    assert refused.status_code == 403
    assert "suspended" in refused.json()["detail"]


async def test_an_expired_approval_can_no_longer_be_answered(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    run, approval = await api.asked(alice)
    await api.runtime.runtime.human.expire(datetime.now(UTC) + timedelta(days=2))
    assert (await _approve(api, approval, lead)).status_code == 409
    assert await _finished(api, run) == "failed"


async def test_a_local_server_can_answer_without_identities(api: Api) -> None:
    """``aios dev`` has no sign-in: the anonymous caller stands in for any approver."""
    runtime = api.runtime.runtime
    transport = httpx2.ASGITransport(app=create_app(runtime))
    alice = await api.bearer("alice", "member")
    run, approval = await api.asked(alice)
    async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as local:
        headers = {"X-AIOS-Tenant": "acme"}
        listed = (await local.get("/v1/approvals", headers=headers)).json()
        assert [entry["approval"] for entry in listed] == [approval]
        answered = await local.post(f"/v1/approvals/{approval}/approve", headers=headers)
    assert answered.status_code == 200, answered.text
    assert await _finished(api, run) == "completed"


async def test_a_stored_answer_is_counted_with_how_long_it_took(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    run, approval = await api.asked(alice)
    await _approve(api, approval, lead)
    await _finished(api, run)
    async with asyncio.timeout(5):
        while True:
            text = api.metrics.exposition().decode()
            if 'aios_approvals_total{app="desk",outcome="granted",tenant="acme"}' in text:
                break
            await asyncio.sleep(0.01)
    assert 'aios_approval_wait_seconds_count{app="desk",outcome="granted",tenant="acme"} 1' in text


async def test_a_run_says_what_it_has_read(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    run, approval = await api.asked(alice)
    before = (await api.http.get(f"/v1/apps/desk/runs/{run}", headers=alice)).json()
    assert (before["tainted"], before["taint"]) == (False, [])
    await _approve(api, approval, lead)
    await _finished(api, run)
    after = (await api.http.get(f"/v1/apps/desk/runs/{run}", headers=alice)).json()
    assert (after["tainted"], after["taint"]) == (True, ["tool:pay.refund.create"])


async def test_a_run_list_shows_the_newest_first(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    first, approval = await api.asked(alice)
    await _approve(api, approval, lead)
    await _finished(api, first)
    second, _ = await api.asked(alice)
    runs = (await api.http.get("/v1/apps/desk/runs", headers=alice)).json()
    assert [run["run_id"] for run in runs] == [second, first]
    latest, earlier = runs
    assert (latest["status"], earlier["status"]) == ("paused", "completed")
    assert earlier["workflow"] == latest["workflow"] != "None"
    assert earlier["on_behalf_of"].startswith("service:alice")
    assert earlier["started_at"] <= earlier["updated_at"]


async def test_a_run_list_can_be_limited(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    await api.asked(alice)
    second, _ = await api.asked(alice)
    runs = (await api.http.get("/v1/apps/desk/runs?limit=1", headers=alice)).json()
    assert [run["run_id"] for run in runs] == [second]


async def test_a_run_list_is_for_one_tenant(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    await api.asked(alice)
    gina = await api.bearer("gina", "member", tenant="globex")
    assert (await api.http.get("/v1/apps/desk/runs", headers=gina)).json() == []


async def test_a_run_list_needs_a_credential(api: Api) -> None:
    assert (await api.http.get("/v1/apps/desk/runs")).status_code == 401


async def test_an_unknown_app_has_no_run_list(api: Api) -> None:
    alice = await api.bearer("alice", "member")
    assert (await api.http.get("/v1/apps/nobody/runs", headers=alice)).status_code == 404


async def test_a_caller_can_ask_who_they_are(api: Api) -> None:
    lead = await api.bearer("lead", "member", "lead")
    me = (await api.http.get("/v1/me", headers=lead)).json()
    assert me == {"principal": "service:lead@acme", "tenant": "acme", "roles": ["lead", "member"]}


async def test_the_console_gets_what_it_shows(api: Api) -> None:
    """console/src/lib/types.ts reads these fields. A rename here breaks the inbox there."""
    alice = await api.bearer("alice", "member")
    lead = await api.bearer("lead", "member", "lead")
    _, approval = await api.asked(alice)
    found = (await api.http.get(f"/v1/approvals/{approval}", headers=lead)).json()
    expected = {
        "approval", "app", "run", "status", "summary", "action", "risk", "payload", "rationale",
        "tainted", "taint", "requested_by", "initiator", "requested_at", "expires_at", "data_class",
        "policies", "by", "comment", "decided_at", "trace",
    }  # fmt: skip
    assert expected <= set(found), expected - set(found)

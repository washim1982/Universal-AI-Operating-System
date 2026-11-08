"""Slack, Teams and email with signed action links (FR-HUM-07; WP 3.5; M3 exit 5)."""

import asyncio
import json
import re
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from types import ModuleType
from typing import Any

import httpx2
import pytest
from pydantic import JsonValue

from aios.api import create_app
from aios.core.human import Approval
from aios.core.identity import Identity
from aios.core.manifests import load_manifests
from aios.core.policy import SideEffect
from aios.fabric import Scope
from aios.human import (
    ActionLink,
    ActionLinks,
    Contact,
    EmailChannel,
    SlackChannel,
    TeamsChannel,
)
from aios.human.channels import recipients
from aios.kernel import Context
from aios.security.identity import ApiKeys
from aios.testing.runtime import LocalRuntime, RunHandle
from aios.tools import NativeProvider, tool

SECRET = "XXXXXX"
PUBLIC = "https://aios.example"
VAULT = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: vault, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: pay}]
  agents: [clerk]
  workflows: [pay]
  policies: [payments]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: clerk, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "channels_vault:pay"}
  capabilities: [{name: vault.pay}]
  tools: {allow: ["vault.>"]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: pay, version: 1.0.0}
spec:
  steps:
    - {id: pay, type: task, capability: vault.pay, input: {amount: 900}}
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: payments, version: 1.0.0}
spec:
  subject: {app: vault}
  allow:
    - {action: tool.invoke, resource: vault.transfer.create}
  requireApproval:
    - action: tool.invoke
      resource: vault.transfer.create
      approvers: ["group:finance", "role:cfo"]
%s
"""
ANN = Identity("user:ann@acme", "acme", frozenset({"member"}))
BOB = Identity("user:bob@acme", "acme", frozenset({"member"}))
CFO = Identity("user:cfo@acme", "acme", frozenset({"member", "cfo"}))
DANA = Identity("user:dana@acme", "acme", frozenset({"member"}))
ADMIN = Identity("system:platform", "acme")


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while True:
            if check():
                return
            await asyncio.sleep(0.02)


async def _pay(ctx: Context, task: dict[str, Any]) -> Any:
    return await ctx.tools.invoke("vault.transfer.create", amount=task["amount"])


@dataclass
class Slack:
    """A stand-in for Slack's Web API: it keeps what it is sent."""

    messages: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    tokens: list[str] = field(default_factory=list[str])
    fail: bool = False

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/chat.postMessage"
        self.tokens.append(request.headers["Authorization"])
        if self.fail:
            return httpx2.Response(200, json={"ok": False, "error": "channel_not_found"})
        self.messages.append(json.loads(request.content))
        return httpx2.Response(200, json={"ok": True, "ts": "1.2"})

    def links(self, to: str) -> dict[str, str]:
        """The buttons of the last message to ``to``."""
        message = [m for m in self.messages if m["channel"] == to][-1]
        buttons = message["blocks"][1]["elements"]
        return {button["action_id"]: button["url"] for button in buttons}


@dataclass
class Vault:
    runtime: LocalRuntime
    slack: Slack
    mail: list[EmailMessage]
    teams: list[dict[str, Any]]
    sent: list[float]
    http: httpx2.AsyncClient

    async def asked(self) -> tuple[RunHandle, Approval]:
        """Dana asks for a payment, and it waits for an approval."""
        handle = await self.runtime.chat("vault", "pay it", principal=DANA.principal)
        event = await handle.wait_for_event("approval.requested", timeout=10)
        data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
        human = self.runtime.runtime.human
        async with asyncio.timeout(10):
            while True:
                if (found := await human.get("acme", data["approval"])) is not None:
                    return handle, found
                await asyncio.sleep(0.02)

    async def told(self, count: int) -> None:
        await until(lambda: len(self.slack.messages) >= count)

    async def follow(self, url: str, comment: str | None = None) -> httpx2.Response:
        path = url.removeprefix(PUBLIC)
        body = {"comment": comment} if comment else {}
        return await self.http.post(path, data=body)

    async def events(self, run: str, type: str) -> list[dict[str, Any]]:
        bus = self.runtime.runtime.bus
        found = [s.event async for s in bus.replay(Scope("acme", "vault"), type, run_id=run)]
        return [e.data for e in found if isinstance(e.data, dict)]


@pytest.fixture
async def vault(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Vault]:
    module = ModuleType("channels_vault")
    module.pay = _pay  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "channels_vault", module)
    slack, mail, teams, sent = Slack(), list[EmailMessage](), list[dict[str, Any]](), list[float]()

    async def deliver(message: EmailMessage) -> None:
        mail.append(message)

    def teams_handle(request: httpx2.Request) -> httpx2.Response:
        teams.append(json.loads(request.content))
        return httpx2.Response(202)

    channels = [
        SlackChannel(
            "xoxb-test", api="https://slack.test/api", transport=httpx2.MockTransport(slack.handle)
        ),
        TeamsChannel("https://teams.test/flow", transport=httpx2.MockTransport(teams_handle)),
        EmailChannel("aios@acme.example", deliver),
    ]

    @tool("vault.transfer.create", side_effect=SideEffect.FINANCIAL, trusted=True)
    async def transfer(amount: float) -> dict[str, Any]:
        sent.append(amount)
        return {"sent": amount}

    links = ActionLinks(SECRET, PUBLIC)
    async with LocalRuntime(tenant="acme", channels=channels, links=links) as runtime:
        core = runtime.runtime
        await core.register_tool_provider(NativeProvider("vault", [transfer]))
        await core.apply(load_manifests(VAULT % ""), tenant="acme")
        await core.set_group(ADMIN, "finance", ["user:ann@acme", "user:bob@acme", DANA.principal])
        for person in (ANN, BOB, CFO, DANA):
            await core.security.set_roles("acme", person.principal, person.roles)
        await core.set_contact(ANN, Contact(slack="U0ANN0001"))
        await core.set_contact(CFO, Contact(email="cfo@acme.example", slack="U0CFO0001"))
        await core.set_contact(DANA, Contact(slack="U0DANA001"))
        app = create_app(core, authenticator=ApiKeys(core.security))
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=PUBLIC) as http:
            yield Vault(runtime, slack, mail, teams, sent, http)


# ---------------------------------------------------------------------- M3 exit 5


async def test_an_approval_is_completed_from_slack_with_a_signed_link(vault: Vault) -> None:
    handle, approval = await vault.asked()
    await vault.told(2)
    assert {m["channel"] for m in vault.slack.messages} == {"U0ANN0001", "U0CFO0001"}
    assert vault.slack.tokens[0] == "Bearer xoxb-test"
    message = vault.slack.messages[0]
    assert message["text"] == "vault asks you to approve vault.transfer.create"
    approve = vault.slack.links("U0ANN0001")["approve"]
    assert approve.startswith(f"{PUBLIC}/v1/act/v1.")

    page = await vault.http.get(approve.removeprefix(PUBLIC))  # what Slack's button opens
    assert page.status_code == 200
    assert "Approve vault.transfer.create?" in page.text
    assert "user:ann@acme" in page.text
    assert page.headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert (await vault.runtime.runtime.human.get("acme", approval.id)).status == "pending"  # type: ignore[union-attr]

    answered = await vault.follow(approve, comment="matches the invoice")
    assert answered.status_code == 200, answered.text
    assert "Approved" in answered.text
    assert (await handle.wait(timeout=10)).status == "completed"
    assert vault.sent == [900]
    [granted] = await vault.events(handle.run_id, "approval.granted")
    assert (granted["by"], granted["via"], granted["comment"]) == (
        "user:ann@acme",
        "slack",
        "matches the invoice",
    )


# ---------------------------------------------------------------------- who is told, and how


async def test_each_approver_is_told_where_they_said(vault: Vault) -> None:
    _, approval = await vault.asked()
    await vault.told(2)
    await until(lambda: bool(vault.mail and vault.teams))
    # bob gave no address; ann gave only Slack; the CFO gave Slack and email
    [mail] = vault.mail
    assert (mail["To"], mail["From"]) == ("cfo@acme.example", "aios@acme.example")
    assert mail["Subject"] == "vault asks you to approve vault.transfer.create"
    body = mail.get_content()
    assert f"{PUBLIC}/v1/act/v1." in body
    assert "vault.transfer.create(amount=900)" in body  # internal data: the summary is shown
    [card] = vault.teams
    assert card["recipient"] == "cfo@acme.example"
    actions = card["attachments"][0]["content"]["actions"]
    assert [a["title"] for a in actions] == ["Approve", "Deny"]
    assert approval.id  # each is the same approval


async def test_whoever_an_action_is_for_isnt_asked(vault: Vault) -> None:
    core = vault.runtime.runtime
    _, approval = await vault.asked()
    await vault.told(2)
    assert approval.initiator == DANA.principal
    found = await recipients(
        approval.details.approvers,
        "acme",
        groups=await core.groups("acme"),
        holders=core.security.holders,
    )
    assert found == ["user:ann@acme", "user:bob@acme", DANA.principal, "user:cfo@acme"]
    await asyncio.sleep(0.2)
    assert "U0DANA001" not in {m["channel"] for m in vault.slack.messages}


async def test_a_deny_link_denies(vault: Vault) -> None:
    handle, _ = await vault.asked()
    await vault.told(2)
    denied = await vault.follow(vault.slack.links("U0CFO0001")["deny"])
    assert denied.status_code == 200
    assert "Denied" in denied.text
    await handle.wait(timeout=10)
    assert vault.sent == []
    [event] = await vault.events(handle.run_id, "approval.denied")
    assert (event["by"], event["via"]) == ("user:cfo@acme", "slack")


async def test_a_link_works_once(vault: Vault) -> None:
    handle, _ = await vault.asked()
    await vault.told(2)
    approve = vault.slack.links("U0ANN0001")["approve"]
    assert (await vault.follow(approve)).status_code == 200
    again = await vault.follow(approve)
    assert again.status_code == 409
    page = await vault.http.get(approve.removeprefix(PUBLIC))
    assert "already granted" in page.text
    await handle.wait(timeout=10)


async def test_a_forged_or_changed_link_is_refused(vault: Vault) -> None:
    _, approval = await vault.asked()
    await vault.told(2)
    approve = vault.slack.links("U0ANN0001")["approve"]
    version, body, mac = approve.rsplit("/", 1)[1].split(".")
    other = ActionLinks("another-secret-that-is-long-enough-too", PUBLIC)
    expires = datetime.now(UTC) + timedelta(hours=1)
    forged = other.url(ActionLink("acme", approval.id, True, "user:ann@acme", "slack", expires))
    for url in (
        forged,
        f"{PUBLIC}/v1/act/{version}.{body}.{mac[:-2]}xx",
        f"{PUBLIC}/v1/act/{version}.{body[:-2]}xx.{mac}",
        f"{PUBLIC}/v1/act/nonsense",
    ):
        assert (await vault.follow(url)).status_code == 401, url
        assert (await vault.http.get(url.removeprefix(PUBLIC))).status_code == 401
    assert (await vault.runtime.runtime.human.get("acme", approval.id)).status == "pending"  # type: ignore[union-attr]


async def test_a_link_for_someone_the_rule_doesnt_name_answers_nothing(vault: Vault) -> None:
    _, approval = await vault.asked()
    links = ActionLinks(SECRET, PUBLIC)  # signed by this server, but for the wrong person
    expires = datetime.now(UTC) + timedelta(hours=1)
    url = links.url(ActionLink("acme", approval.id, True, "user:eve@acme", "slack", expires))
    assert (await vault.follow(url)).status_code == 403
    for_the_initiator = links.url(
        ActionLink("acme", approval.id, True, str(approval.initiator), "slack", expires)
    )
    assert (await vault.follow(for_the_initiator)).status_code == 403
    assert (await vault.runtime.runtime.human.get("acme", approval.id)).status == "pending"  # type: ignore[union-attr]


async def test_a_link_answers_with_the_roles_its_person_has_now(vault: Vault) -> None:
    core = vault.runtime.runtime
    _, approval = await vault.asked()
    await vault.told(2)
    await core.security.set_roles("acme", "user:cfo@acme", frozenset({"member"}))  # no longer CFO
    assert (await vault.follow(vault.slack.links("U0CFO0001")["approve"])).status_code == 403
    assert (await core.human.get("acme", approval.id)).status == "pending"  # type: ignore[union-attr]


async def test_escalation_and_reminders_tell_people_again(vault: Vault) -> None:
    core = vault.runtime.runtime
    terms = (
        '      remindEvery: 30m\n      escalate:\n        - {after: 2h, approvers: ["role:cfo"]}'
    )
    finance_only = VAULT.replace('["group:finance", "role:cfo"]', '["group:finance"]')
    await core.apply(load_manifests(finance_only % terms), tenant="acme")
    _, approval = await vault.asked()
    await vault.told(1)
    assert [m["channel"] for m in vault.slack.messages] == ["U0ANN0001"]
    start = approval.request.time
    await core.human.nudge(start + timedelta(minutes=31))
    await vault.told(2)
    assert vault.slack.messages[1]["text"].startswith("Reminder: ")
    await core.human.nudge(start + timedelta(hours=2))
    await vault.told(3)
    assert vault.slack.messages[2]["channel"] == "U0CFO0001"  # only the newly asked
    assert vault.slack.messages[2]["text"].startswith("Escalated to you: ")


async def test_a_message_that_cant_be_sent_is_tried_again(vault: Vault) -> None:
    vault.slack.fail = True
    await vault.asked()
    await until(lambda: len(vault.slack.tokens) >= 4)  # two people, at least twice each
    vault.slack.fail = False
    await vault.told(2)


async def test_restricted_data_stays_out_of_messages() -> None:
    event_data: dict[str, JsonValue] = {
        "approval": "01J0000000000000000000000A",
        "approvers": ["user:ann@acme"],
        "reason": "r",
        "summary": "hr.salary.set(employee=ann, salary=99000)",
        "risk": "high",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    from aios.core.envelope import DataClass, Event  # noqa: PLC0415
    from aios.human.channels import Notice  # noqa: PLC0415

    request = Event(
        source="aios://acme/system/human",
        type="approval.requested",
        tenantid="acme",
        appid="hr",
        principal="agent:hr@acme",
        data=event_data,
        dataclass=DataClass.RESTRICTED,
    )
    notice = Notice("requested", Approval.of(request), "user:ann@acme", Contact(), "a", "d")
    assert all("salary" not in line for line in notice.lines)


# ---------------------------------------------------------------------- links themselves


def test_a_link_says_who_what_and_until_when() -> None:
    links = ActionLinks(SECRET, PUBLIC + "/")
    expires = datetime(2030, 1, 1, tzinfo=UTC)
    link = ActionLink(
        "acme", "01J0000000000000000000000A", False, "user:ann@acme", "email", expires
    )
    token = links.token(link)
    assert re.fullmatch(r"v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}", token)
    assert links.read(token, now=expires - timedelta(seconds=1)) == link
    assert links.read(token, now=expires) is None  # expired
    assert links.url(link) == f"{PUBLIC}/v1/act/{token}"


@pytest.mark.parametrize(
    ("secret", "url"),
    [("too-short", PUBLIC), (SECRET, "aios.example"), (SECRET, "ftp://aios.example")],
)
def test_links_need_a_long_secret_and_a_web_address(secret: str, url: str) -> None:
    with pytest.raises(ValueError, match=r"secret|URL"):
        ActionLinks(secret, url)


@pytest.mark.parametrize(
    "contact",
    [{"email": "not an address"}, {"slack": "ann"}, {"slack": "u0ann0001"}],
)
def test_a_contact_must_look_right(contact: dict[str, str]) -> None:
    with pytest.raises(ValueError, match=r"email|Slack"):
        Contact(**contact)


# ---------------------------------------------------------------------- setting your own


async def test_people_set_their_own_contact_and_nobody_elses(vault: Vault) -> None:
    core = vault.runtime.runtime
    _, key = await core.create_key(ADMIN, "dana", ["member"])
    headers = {"Authorization": f"Bearer {key}"}
    assert (await vault.http.get("/v1/me/contact", headers=headers)).json() == {
        "email": None,
        "slack": None,
    }
    put = await vault.http.put(
        "/v1/me/contact", headers=headers, json={"email": "dana@acme.example"}
    )
    assert put.status_code == 200, put.text
    assert (await vault.http.get("/v1/me/contact", headers=headers)).json()["email"] == (
        "dana@acme.example"
    )
    bad = await vault.http.put("/v1/me/contact", headers=headers, json={"slack": "dana"})
    assert bad.status_code == 422
    # an event claiming to set someone else's is ignored
    from aios.core.catalog import ContactChanged  # noqa: PLC0415

    await core._security_event(  # pyright: ignore[reportPrivateUsage]
        Identity("user:mallory@acme", "acme"),
        "security.contact.changed",
        ContactChanged(principal="user:ann@acme", email="mallory@evil.example"),
    )
    assert (await core.contacts("acme"))["user:ann@acme"] == Contact(slack="U0ANN0001")
    await core.set_contact(ANN, Contact())
    assert "user:ann@acme" not in await core.contacts("acme")

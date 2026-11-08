"""Slack, Microsoft Teams and email: each approver is told, with links of their own (FR-HUM-07).

* **A link is for one person, one approval and one answer.** It says so itself, signed with
  HMAC-SHA256 under the server's link secret, and stops working when the approval would expire, or
  after seven days at most. Whoever holds a link answers as that person, so it goes to them alone:
  a direct message or an email, never a shared channel.
* **Opening a link decides nothing.** It shows what is asked and a button. Only pressing the button
  (a POST) answers. Link previews and mail scanners open links, and they must not approve refunds.
* **The answer is checked like any other.** The person must still be someone the approval is for,
  with the roles they have now, in a tenant and App that aren't suspended. Whoever an action is for
  can't approve it, and nobody counts twice towards a quorum.
* **A message says little.** It names the App, the action, the risk and when it expires, and gives
  the one-line summary only when the request's data is public or internal. The rest is on the page
  AIOS serves, to whoever holds the link.
* **At least once.** If a message can't be sent, they are all tried again, so someone may be told
  twice. Nobody should be told never.

People give their own addresses (``PUT /v1/me/contact``). An administrator can't, because an
address set by someone else would send that person's links to them.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import smtplib
import ssl
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Literal, cast

import httpx2

from aios.core.envelope import DataClass, Event
from aios.core.human import Approval, Groups
from aios.human.store import ApprovalStore

logger = logging.getLogger("aios.human")

TIMEOUT_S = 10.0
LONGEST_LINK = timedelta(days=7)
MIN_SECRET = 32
TOKEN_VERSION = "v1"
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}")
SLACK_MEMBER = re.compile(r"[UW][A-Z0-9]{2,20}")
SHOWN = (DataClass.PUBLIC, DataClass.INTERNAL)
"""Data classes whose summary a message may carry outside AIOS."""

Why = Literal["requested", "reminded", "escalated"]


@dataclass(frozen=True, slots=True)
class Contact:
    """Where a person can be told. They set it themselves."""

    email: str | None = None
    slack: str | None = None
    """A Slack member id (``U0123ABCD``): Profile, then the three dots, then *Copy member ID*."""

    def __post_init__(self) -> None:
        if self.email is not None and not EMAIL.fullmatch(self.email):
            raise ValueError("that isn't an email address")
        if self.slack is not None and not SLACK_MEMBER.fullmatch(self.slack):
            raise ValueError("a Slack member id looks like U0123ABCD")


Contacts = Mapping[str, Contact]
ContactsOf = Callable[[str], Awaitable[Contacts]]
GroupsOf = Callable[[str], Awaitable[Groups]]
HoldersOf = Callable[[str, str], Awaitable[Collection[str]]]
"""The principals of a tenant that hold a role."""


# ---------------------------------------------------------------------- signed links


@dataclass(frozen=True, slots=True)
class ActionLink:
    tenant: str
    approval: str
    approve: bool
    principal: str
    """Who answers by following it."""
    channel: str
    """Where it was sent, for the record of the answer."""
    expires: datetime


class ActionLinks:
    """Makes and checks the links. Changing the secret ends every link already sent."""

    def __init__(self, secret: str, base_url: str) -> None:
        if len(secret) < MIN_SECRET:
            raise ValueError(f"an action link secret needs at least {MIN_SECRET} characters")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("the public URL starts with https:// (or http:// on one machine)")
        self._key = hashlib.sha256(b"aios action link\x00" + secret.encode()).digest()
        self.base_url = base_url.rstrip("/")

    def token(self, link: ActionLink) -> str:
        claims = {
            "t": link.tenant,
            "a": link.approval,
            "v": "approve" if link.approve else "deny",
            "p": link.principal,
            "c": link.channel,
            "e": int(link.expires.timestamp()),
        }
        body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        return f"{TOKEN_VERSION}.{body}.{self._mac(body)}"

    def url(self, link: ActionLink) -> str:
        return f"{self.base_url}/v1/act/{self.token(link)}"

    def read(self, token: str, now: datetime | None = None) -> ActionLink | None:
        """The link ``token`` is, if this server signed it and it hasn't expired."""
        version, _, rest = token.partition(".")
        body, _, mac = rest.partition(".")
        if version != TOKEN_VERSION or not body or not hmac.compare_digest(mac, self._mac(body)):
            return None
        try:
            claims = cast(dict[str, Any], json.loads(_unb64(body)))
            link = ActionLink(
                tenant=str(claims["t"]),
                approval=str(claims["a"]),
                approve={"approve": True, "deny": False}[str(claims["v"])],
                principal=str(claims["p"]),
                channel=str(claims["c"]),
                expires=datetime.fromtimestamp(int(claims["e"]), UTC),
            )
        except (ValueError, KeyError, TypeError):
            return None
        return link if (now or datetime.now(UTC)) < link.expires else None

    def _mac(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------- what is sent


@dataclass(frozen=True, slots=True)
class Notice:
    """What one channel tells one person about one approval."""

    why: Why
    approval: Approval
    principal: str
    contact: Contact
    approve: str
    deny: str

    @property
    def headline(self) -> str:
        what = self.approval.details.action or "an action"
        app = self.approval.app
        return {
            "requested": f"{app} asks you to approve {what}",
            "reminded": f"Reminder: {app} is still waiting for you to approve {what}",
            "escalated": f"Escalated to you: {app} asks you to approve {what}",
        }[self.why]

    @property
    def lines(self) -> list[str]:
        details = self.approval.details
        lines: list[str] = []
        if self.approval.request.dataclass in SHOWN:
            lines.append(details.summary)
        lines.append(f"Risk: {details.risk}")
        if details.quorum > 1:
            lines.append(f"Needs {details.quorum} approvers; {len(self.approval.votes)} so far")
        lines.append(f"Expires: {details.expires_at:%Y-%m-%d %H:%M} UTC")
        return lines


class Channel(ABC):
    name: str

    @abstractmethod
    async def send(self, notice: Notice) -> bool:
        """Tell one person. ``False``: they have no address here. Raises if it couldn't be sent."""


def _slack_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackChannel(Channel):
    """A direct message from a Slack app's bot (``chat:write``), with Approve and Deny buttons.

    The buttons are links, so the app needs no request URL and AIOS needs no inbound access from
    Slack: the person's browser opens the page AIOS serves.
    """

    name = "slack"

    def __init__(
        self,
        token: str,
        *,
        api: str = "https://slack.com/api",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.token = token
        self.api = api.rstrip("/")
        self.transport = transport

    async def send(self, notice: Notice) -> bool:
        if notice.contact.slack is None:
            return False
        text = "\n".join(_slack_text(line) for line in notice.lines)
        message = {
            "channel": notice.contact.slack,
            "text": notice.headline,
            "unfurl_links": False,
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{_slack_text(notice.headline)}*\n{text}"},
                },
                {
                    "type": "actions",
                    "elements": [
                        _slack_button("Approve", notice.approve, "primary"),
                        _slack_button("Deny", notice.deny, "danger"),
                    ],
                },
            ],
        }
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx2.AsyncClient(timeout=TIMEOUT_S, transport=self.transport) as http:
            response = await http.post(
                f"{self.api}/chat.postMessage", json=message, headers=headers
            )
        response.raise_for_status()
        answer = cast(dict[str, Any], response.json())
        if not answer.get("ok"):
            raise RuntimeError(f"Slack refused the message: {answer.get('error')}")
        return True


def _slack_button(text: str, url: str, style: str) -> dict[str, Any]:
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "url": url,
        "style": style,
        "action_id": text.lower(),
    }


class TeamsChannel(Channel):
    """An Adaptive Card for a Teams workflow (``When a Teams webhook request is received``, then
    ``Post card in a chat or channel`` to the ``recipient``), with Approve and Deny buttons."""

    name = "teams"

    def __init__(self, url: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self.url = url
        self.transport = transport

    async def send(self, notice: Notice) -> bool:
        if notice.contact.email is None:
            return False
        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {"type": "TextBlock", "text": notice.headline, "weight": "Bolder", "wrap": True},
                *({"type": "TextBlock", "text": line, "wrap": True} for line in notice.lines),
            ],
            "actions": [
                {"type": "Action.OpenUrl", "title": "Approve", "url": notice.approve},
                {"type": "Action.OpenUrl", "title": "Deny", "url": notice.deny},
            ],
        }
        message = {
            "type": "message",
            "recipient": notice.contact.email,
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
            ],
        }
        async with httpx2.AsyncClient(timeout=TIMEOUT_S, transport=self.transport) as http:
            response = await http.post(self.url, json=message)
        response.raise_for_status()
        return True


Deliver = Callable[[EmailMessage], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Smtp:
    """Sends mail through a server, with STARTTLS unless told otherwise."""

    host: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    tls: Literal["starttls", "ssl", "none"] = "starttls"

    async def __call__(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self._send, message)

    def _send(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        server = (
            smtplib.SMTP_SSL(self.host, self.port, timeout=TIMEOUT_S, context=context)
            if self.tls == "ssl"
            else smtplib.SMTP(self.host, self.port, timeout=TIMEOUT_S)
        )
        with server:
            if self.tls == "starttls":
                server.starttls(context=context)
            if self.username:
                server.login(self.username, self.password or "")
            server.send_message(message)


class EmailChannel(Channel):
    name = "email"

    def __init__(self, sender: str, deliver: Deliver) -> None:
        self.sender = sender
        self.deliver = deliver

    async def send(self, notice: Notice) -> bool:
        if notice.contact.email is None:
            return False
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = notice.contact.email
        message["Subject"] = notice.headline
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(
            "\n".join(
                [
                    *notice.lines,
                    "",
                    f"Approve: {notice.approve}",
                    f"Deny: {notice.deny}",
                    "",
                    "Each link opens a page that asks you to confirm. It is for you alone: "
                    "whoever has it can answer as you until it expires.",
                ]
            )
        )
        await self.deliver(message)
        return True


# ---------------------------------------------------------------------- who is told


async def recipients(
    selectors: Sequence[str], tenant: str, *, groups: Groups, holders: HoldersOf
) -> list[str]:
    """The people ``selectors`` name. Groups the identity provider asserts can't be listed: name
    them in the tenant's directory, or name people."""
    found: list[str] = []
    for selector in selectors:
        kind, _, name = selector.partition(":")
        if kind == "role":
            found.extend(sorted(await holders(tenant, name)))
        elif kind == "group":
            found.extend(sorted(groups.get(name, ())))
        else:
            found.append(selector if "@" in selector else f"{selector}@{tenant}")
    return list(dict.fromkeys(found))


class ApprovalNotifier:
    """Tells approvers on every channel they have an address for (FR-HUM-05, 07)."""

    def __init__(
        self,
        store: ApprovalStore,
        channels: Sequence[Channel],
        links: ActionLinks,
        *,
        groups: GroupsOf,
        contacts: ContactsOf,
        holders: HoldersOf,
    ) -> None:
        self.store = store
        self.channels = channels
        self.links = links
        self.groups = groups
        self.contacts = contacts
        self.holders = holders

    async def handle(self, event: Event) -> None:
        data = cast(dict[str, Any], event.data) if isinstance(event.data, dict) else {}
        stored = await self.store.get(event.tenantid, str(data.get("approval")))
        why: Why
        if event.type == "approval.requested":
            why, approval = "requested", stored or Approval.of(event)
            selectors: Sequence[str] = approval.details.approvers
        elif event.type == "approval.escalated" and stored is not None:
            why, approval = "escalated", stored
            selectors = cast(list[str], data.get("approvers") or [])
        elif event.type == "approval.reminded" and stored is not None:
            why, approval, selectors = "reminded", stored, stored.approvers
        else:
            return
        if approval.status != "pending":
            return
        groups = await self.groups(approval.tenant)
        people = await recipients(selectors, approval.tenant, groups=groups, holders=self.holders)
        skipped = {approval.initiator, *approval.voted}
        contacts = await self.contacts(approval.tenant)
        failed: list[str] = []
        for principal in people:
            contact = contacts.get(principal)
            if principal in skipped or contact is None:
                continue
            for channel in self.channels:
                notice = self._notice(why, approval, principal, contact, channel.name)
                try:
                    await channel.send(notice)
                except Exception as exc:  # the rest are still told; then all are tried again
                    failed.append(f"{channel.name} to {principal}: {exc}")
        if failed:
            raise RuntimeError("; ".join(failed))

    def _notice(
        self, why: Why, approval: Approval, principal: str, contact: Contact, channel: str
    ) -> Notice:
        expires = min(approval.expires_at, datetime.now(UTC) + LONGEST_LINK)

        def link(approve: bool) -> str:
            return self.links.url(
                ActionLink(approval.tenant, approval.id, approve, principal, channel, expires)
            )

        return Notice(why, approval, principal, contact, approve=link(True), deny=link(False))

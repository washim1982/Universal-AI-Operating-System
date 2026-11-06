"""Tells another system that an approval needs attention (FR-HUM-07).

A webhook carries no payload and no rationale: only what it takes to find the approval. The
receiver fetches the rest through the API with its own credential, so ``restricted`` content never
leaves in a notification, and a leaked webhook log shows nobody's refund.

Requests are signed like this: ``X-AIOS-Signature: t=<unix seconds>,v1=<hex>``, where ``v1`` is
HMAC-SHA256 over ``<t>.<body>``. Covering the time lets a receiver refuse an old request replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx2

from aios.core.catalog import ApprovalRequested
from aios.core.envelope import Event

TIMEOUT_S = 10.0


def signature(secret: str, timestamp: int, body: bytes) -> str:
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def signed_by(secret: str, header: str | None, body: bytes, *, tolerance: float) -> str | None:
    """The request's own signature, if ``header`` proves ``secret`` signed ``body`` just now."""
    fields = dict(part.split("=", 1) for part in (header or "").split(",") if "=" in part)
    stamp, given = fields.get("t", ""), fields.get("v1")
    if not stamp.isdigit() or given is None or abs(time.time() - int(stamp)) > tolerance:
        return None
    expected = signature(secret, int(stamp), body).rsplit("v1=", 1)[1]
    return given if hmac.compare_digest(expected, given) else None


def notice(event: Event) -> dict[str, object]:
    """What a webhook says about one ``approval.*`` event."""
    data: dict[str, object] = event.data if isinstance(event.data, dict) else {}  # type: ignore[assignment]
    approval = str(data.get("approval"))
    message: dict[str, object] = {
        "type": event.type,
        "approval": approval,
        "tenant": event.tenantid,
        "app": event.appid,
        "run": event.runid,
        "time": event.time.isoformat(),
        "link": f"/v1/approvals/{approval}",
    }
    if event.type == "approval.requested":
        request = ApprovalRequested.model_validate(data)
        message.update(
            action=request.action,
            step=request.step,
            risk=request.risk,
            approvers=request.approvers,
            expires_at=request.expires_at.isoformat(),
        )
    return message


class ApprovalWebhook:
    def __init__(
        self, url: str, secret: str, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self.url = url
        self.secret = secret
        self.transport = transport

    async def send(self, event: Event) -> None:
        """Raises if the receiver didn't take it: the delivery is then tried again."""
        body = json.dumps(notice(event), separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "X-AIOS-Signature": signature(self.secret, int(time.time()), body),
            "X-AIOS-Event": event.id,
        }
        async with httpx2.AsyncClient(timeout=TIMEOUT_S, transport=self.transport) as http:
            response = await http.post(self.url, content=body, headers=headers)
        response.raise_for_status()

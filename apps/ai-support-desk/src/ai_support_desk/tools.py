"""Stand-ins for Stripe and Zendesk, so the desk can be tried with no accounts. No money moves.

A deployment replaces these with its own tool providers. Set
``AIOS_TOOL_PROVIDERS=ai_support_desk.tools:providers`` on the worker to use them. A refund is a
line in a file, so it is easy to count, and a repeat of the same key changes nothing, as with
Stripe's own idempotency keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aios.core.ids import new_ulid
from aios.core.policy import SideEffect
from aios.tools import NativeProvider, ToolProvider, tool

DEFAULT_LEDGER = Path(".aios") / "support-desk" / "refunds.jsonl"


def refunds_in(ledger: Path) -> list[dict[str, Any]]:
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text("utf-8").splitlines()]


def providers(ledger: Path = DEFAULT_LEDGER) -> list[ToolProvider]:
    @tool(
        "stripe.refund.create",
        side_effect=SideEffect.FINANCIAL,
        description="Refund an order, in USD.",
        trusted=True,  # a receipt we made, with nobody else's words in it
    )
    async def refund(
        amountUSD: float,
        order: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        for earlier in refunds_in(ledger):
            if idempotency_key is not None and earlier["key"] == idempotency_key:
                return earlier
        receipt = {
            "id": f"re_{new_ulid().lower()}",
            "order": order,
            "amountUSD": amountUSD,
            "key": idempotency_key,
        }
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as file:
            file.write(json.dumps(receipt) + "\n")
        return receipt

    @tool(
        "zendesk.ticket.comment",
        side_effect=SideEffect.EXTERNAL_COMM,
        description="Post a reply on a ticket, which the customer sees.",
        trusted=True,
    )
    async def comment(ticket: str, text: str) -> dict[str, str]:
        return {"ticket": ticket, "status": "posted"}

    return [NativeProvider("stripe", [refund]), NativeProvider("zendesk", [comment])]

"""One agent that issues a refund, if a person agrees."""

import re
from typing import Any

from aios.core.errors import ApprovalDeniedError
from aios.kernel import Context, agent

_ORDER = re.compile(r"order\s+(\d+)", re.IGNORECASE)
_AMOUNT = re.compile(r"\$\s*(\d+(?:\.\d+)?)")


@agent("refund-agent", capability="refunds.issue")
async def issue(ctx: Context, task: dict[str, Any]) -> dict[str, Any]:
    message = str(task.get("message", ""))
    order = _ORDER.search(message)
    amount = _AMOUNT.search(message)
    if order is None or amount is None:
        return {"reply": "Which order, and how much? Try: Refund order 1001 $120"}
    await ctx.emit("refund.proposed", {"order": order[1], "amountUSD": float(amount[1])})
    try:
        receipt = await ctx.tools.invoke(
            "filesystem.file.write",
            path=f"refunds/{ctx.run_id}-{order[1]}.txt",
            content=f"refunded ${amount[1]} for order {order[1]}\n",
            idempotency_key=ctx.step_key("refund"),
            rationale=f"The customer asked: {message}",
        )
    except ApprovalDeniedError as no:
        return {"reply": f"Order {order[1]} can't be refunded automatically.", "why": no.code}
    return {"reply": f"Refunded ${amount[1]} for order {order[1]}.", "receipt": receipt}

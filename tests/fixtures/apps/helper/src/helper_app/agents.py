"""The helper App's agent: what it does depends on the message."""

import asyncio
from typing import Any

from aios import Context, agent
from aios.core.errors import PolicyDeniedError


@agent("helper-agent", capability="answering")
async def helper(ctx: Context, task: Any) -> Any:
    message = str(task.get("message", ""))
    if message == "refund":
        await ctx.tools.invoke("pay.refund.create", amountUSD=120, rationale="it broke")
        return {"answer": "refunded"}
    if message == "slow":
        await asyncio.sleep(0.6)
        return "done at last"
    if message == "fail":
        raise PolicyDeniedError("refused on purpose")
    if message == "json":
        return {"total": 3, "items": ["a", "b"]}
    return {"answer": f"you said {message} ({len(task.get('messages') or [])} messages)"}

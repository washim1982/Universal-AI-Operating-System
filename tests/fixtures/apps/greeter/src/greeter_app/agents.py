"""One agent that greets, slowly when asked to."""

import asyncio
from typing import Any

from aios.kernel import Context, agent

SLOW_SECONDS = 45


@agent("greeter-agent", capability="greetings.greet")
async def greet(ctx: Context, task: dict[str, Any]) -> dict[str, Any]:
    message = str(task.get("message", ""))
    if "slow" in message:
        await asyncio.sleep(SLOW_SECONDS)
    return {"reply": f"Hello! You said: {message}"}

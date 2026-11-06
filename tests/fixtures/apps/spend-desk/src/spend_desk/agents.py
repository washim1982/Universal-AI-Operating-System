"""One agent, one question."""

from typing import Any

from aios.kernel import Context


async def ask(ctx: Context, task: dict[str, Any]) -> dict[str, str]:
    reply = await ctx.model.generate(messages=str(task["message"]), system="You are careful.")
    return {"reply": reply}

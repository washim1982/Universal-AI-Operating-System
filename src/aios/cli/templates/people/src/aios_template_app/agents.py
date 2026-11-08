"""The agent of Aios Template App: it replies, once a person has reviewed the request."""

from typing import Any

from pydantic import BaseModel

from aios import Context, agent


class Reviewed(BaseModel):
    message: str
    review: dict[str, Any]


class Reply(BaseModel):
    reply: str


@agent("reply-agent", capability="replying")
async def reply(ctx: Context, task: Reviewed) -> Reply:
    decision = "accepted" if task.review.get("accept") else "declined"
    return await ctx.model.generate(
        task="summarizing",
        system=f"Write a short, polite reply. The request was {decision}.",
        messages=[ctx.user(f"{task.message}\n\nReviewer's note: {task.review.get('note', '')}")],
        output=Reply,
    )

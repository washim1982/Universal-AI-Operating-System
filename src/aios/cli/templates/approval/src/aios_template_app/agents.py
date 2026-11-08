"""The agent of Aios Template App: it reads a refund request and refunds the order."""

from pydantic import BaseModel

from aios import Context, agent
from aios.core.errors import ApprovalDeniedError


class Request(BaseModel):
    message: str


class Refund(BaseModel):
    order: str
    amountUSD: float


@agent("refund-agent", capability="refunding")
async def refund(ctx: Context, task: Request) -> dict[str, object]:
    asked = await ctx.model.generate(
        task="planning",
        system="Read the order number and the amount to refund, in USD, from the message.",
        messages=[ctx.user(task.message)],
        output=Refund,
    )
    try:
        receipt = await ctx.tools.invoke(
            "orders.refund.create",
            order=asked.order,
            amountUSD=asked.amountUSD,
            rationale=f"The customer asked: {task.message}",
        )
    except ApprovalDeniedError:
        return {"reply": f"Order {asked.order} can't be refunded automatically."}
    return {
        "reply": f"Refunded ${asked.amountUSD:.2f} for order {asked.order}.",
        "receipt": receipt,
    }

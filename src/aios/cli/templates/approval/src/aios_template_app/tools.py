"""The App's own tool. Start AIOS with it: AIOS_TOOL_PROVIDERS=aios_template_app.tools:providers

This stand-in keeps refunds in memory; a real one would call your payment provider.
"""

from typing import Any

from aios.core.policy import SideEffect
from aios.tools import NativeProvider, ToolProvider, tool

REFUNDED: list[dict[str, Any]] = []


@tool(
    "orders.refund.create",
    side_effect=SideEffect.FINANCIAL,
    description="Refund an order, in USD.",
    trusted=True,  # a receipt this code made, with nobody else's words in it
)
async def refund(order: str, amountUSD: float) -> dict[str, Any]:
    receipt = {"order": order, "refunded": amountUSD}
    REFUNDED.append(receipt)
    return receipt


@refund.previewer
async def what_it_would_do(order: str, amountUSD: float) -> str:
    return f"would refund ${amountUSD:.2f} for order {order}"


def providers() -> list[ToolProvider]:
    return [NativeProvider("orders", [refund])]

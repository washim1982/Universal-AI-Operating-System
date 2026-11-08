"""A stand-in for a payments system, so that a refund can wait for a lead's approval.

``aios dev`` loads it with ``AIOS_TOOL_PROVIDERS=helper_app.tools:providers``.
"""

from aios.core.policy import SideEffect
from aios.tools import NativeProvider, ToolProvider, tool


@tool("pay.refund.create", side_effect=SideEffect.FINANCIAL)
async def create(amountUSD: float) -> dict[str, str]:
    return {"status": "refunded", "amount": f"${amountUSD:g}"}


def providers() -> list[ToolProvider]:
    return [NativeProvider("pay", [create])]

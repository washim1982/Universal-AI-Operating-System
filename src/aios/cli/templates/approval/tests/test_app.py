"""Aios Template App refunds; a big refund waits for a person."""

from pathlib import Path

from aios_template_app.tools import REFUNDED, providers

from aios.core.catalog import ApprovalRequested
from aios.testing.models import ScriptedModel
from aios.testing.runtime import LocalRuntime

APP = Path(__file__).parents[1]


async def test_a_small_refund_happens() -> None:
    model = ScriptedModel({"order": "1001", "amountUSD": 20})
    async with LocalRuntime.from_app(APP, model=model, tool_providers=providers()) as runtime:
        result = await (await runtime.chat("aios-template-app", "Refund order 1001 $20")).wait(
            timeout=30
        )
    assert result.status == "completed", result.error
    assert result.output["reply"] == "Refunded $20.00 for order 1001."


async def test_a_big_refund_waits_for_a_person() -> None:
    REFUNDED.clear()
    model = ScriptedModel({"order": "1002", "amountUSD": 500})
    async with LocalRuntime.from_app(APP, model=model, tool_providers=providers()) as runtime:
        run = await runtime.chat("aios-template-app", "Refund order 1002 $500")
        event = await run.wait_for_event("approval.requested", timeout=30)
        asked = ApprovalRequested.model_validate(event.data)
        assert asked.preview == "would refund $500.00 for order 1002"
        assert REFUNDED == []  # nothing yet
        await runtime.approve(asked.approval, by="user:approver@acme")
        result = await run.wait(timeout=30)
    assert result.status == "completed", result.error
    assert REFUNDED == [{"order": "1002", "refunded": 500}]

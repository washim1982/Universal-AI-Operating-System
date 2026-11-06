"""The refund desk the testing guide uses: a whole App, from its own directory."""

from pathlib import Path

from aios.core.catalog import ApprovalRequested
from aios.testing.runtime import LocalRuntime, RunHandle

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "tests" / "fixtures" / "apps" / "refund-desk"
NAME = "refund-desk"
LEAD = "user:lead@acme"


async def _asked(run: RunHandle) -> str:
    event = await run.wait_for_event("approval.requested", timeout=10)
    return ApprovalRequested.model_validate(event.data).approval


async def test_a_refund_waits_for_a_person_then_happens() -> None:
    async with LocalRuntime.from_app(APP) as runtime:
        run = await runtime.chat(NAME, "Refund order 1001 $120")
        approval = await _asked(run)
        await run.wait_for_event("task.suspended", timeout=10)
        await runtime.approve(approval, by=LEAD)
        result = await run.wait(timeout=10)
    assert result.status == "completed", result.error
    assert result.output["reply"] == "Refunded $120 for order 1001."
    assert result.output["receipt"]["path"] == f"refunds/{run.run_id}-1001.txt"
    assert [e.type for e in result.events].count("tool.call.completed") == 1
    assert [e.type for e in result.events].count("refund.proposed") == 1


async def test_a_denied_refund_gets_a_polite_answer() -> None:
    async with LocalRuntime.from_app(APP) as runtime:
        run = await runtime.chat(NAME, "Refund order 1002 $5000")
        await runtime.deny(await _asked(run), by=LEAD)
        result = await run.wait(timeout=10)
    assert result.status == "completed", result.error
    assert result.output == {
        "reply": "Order 1002 can't be refunded automatically.",
        "why": "approval.denied",
    }


async def test_a_vague_request_asks_nobody() -> None:
    async with LocalRuntime.from_app(APP) as runtime:
        result = await (await runtime.chat(NAME, "I want my money back")).wait(timeout=10)
    assert "Which order" in result.output["reply"]
    assert "approval.requested" not in [e.type for e in result.events]

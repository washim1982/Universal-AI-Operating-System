"""The people desk the testing guide uses: a review by a person, then an approval that can change
what is done (FR-HUM-06, 08)."""

import asyncio
from pathlib import Path

from aios.core.catalog import ApprovalRequested, HumanTaskAssigned
from aios.core.identity import Identity
from aios.testing.runtime import LocalRuntime, RunHandle

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "tests" / "fixtures" / "apps" / "people-desk"
NAME = "people-desk"
LEAD = Identity("user:lead@acme", "acme", frozenset({"member", "support-lead"}))
OTHER = Identity("user:sam@acme", "acme", frozenset({"member", "support-lead"}))


async def _reviewed(runtime: LocalRuntime, run: RunHandle, verdict: str) -> None:
    event = await run.wait_for_event("human.task.assigned", timeout=10)
    task = HumanTaskAssigned.model_validate(event.data).task
    async with asyncio.timeout(10):
        while True:
            if await runtime.runtime.human.task(runtime.tenant, task) is not None:
                break
            await asyncio.sleep(0.02)
    await runtime.runtime.complete_task(LEAD, task, {"verdict": verdict, "note": "order checked"})


async def test_a_reviewed_refund_waits_for_approval_and_can_be_changed() -> None:
    async with LocalRuntime.from_app(APP) as runtime:
        run = await runtime.chat(NAME, "Refund order 1001 $120")
        await _reviewed(runtime, run, "refund")
        event = await run.wait_for_event("approval.requested", timeout=10)
        asked = ApprovalRequested.model_validate(event.data)
        assert "order checked" in str(asked.rationale)
        async with asyncio.timeout(10):
            while True:
                if await runtime.runtime.human.get(runtime.tenant, asked.approval) is not None:
                    break
                await asyncio.sleep(0.02)
        changed = "refunded $60 for order 1001 (half: it arrived late)\n"
        await runtime.runtime.human.decide(
            OTHER, asked.approval, approve=True, edits={"content": changed}
        )
        result = await run.wait(timeout=10)
        written = runtime.sandbox / f"refunds/{run.run_id}-1001.txt"
        assert written.read_text(encoding="utf-8") == changed  # what the approver wrote
    assert result.status == "completed", result.error
    assert result.output["reply"] == "Refunded order 1001."
    # the agent's step is the agent's: people were given the review, and nothing else
    assigned = [e for e in result.events if e.type == "human.task.assigned"]
    assert [e.data["capability"] for e in assigned] == ["refunds.review"]  # type: ignore[index]


async def test_a_refused_review_asks_nobody_to_approve() -> None:
    async with LocalRuntime.from_app(APP) as runtime:
        run = await runtime.chat(NAME, "Refund order 1002 $5000")
        await _reviewed(runtime, run, "refuse")
        result = await run.wait(timeout=10)
    assert result.output == {"reply": "The reviewer said no.", "note": "order checked"}
    assert "approval.requested" not in [e.type for e in result.events]

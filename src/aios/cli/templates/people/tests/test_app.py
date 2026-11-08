"""A person reviews the request in Aios Template App, then the agent replies."""

import asyncio
from pathlib import Path

from aios.core.catalog import HumanTaskAssigned
from aios.core.identity import Identity
from aios.testing.models import ScriptedModel
from aios.testing.runtime import LocalRuntime

APP = Path(__file__).parents[1]
REVIEWER = Identity("user:rita@acme", "acme", frozenset({"member", "reviewer"}))


async def test_a_person_reviews_then_the_agent_replies() -> None:
    model = ScriptedModel({"reply": "Good news: your request is accepted."})
    async with LocalRuntime.from_app(APP, model=model) as runtime:
        run = await runtime.chat("aios-template-app", "Can I change my delivery address?")
        event = await run.wait_for_event("human.task.assigned", timeout=30)
        task = HumanTaskAssigned.model_validate(event.data).task
        async with asyncio.timeout(10):  # until the Human OS has filed it
            while True:
                if await runtime.runtime.human.task(runtime.tenant, task) is not None:
                    break
                await asyncio.sleep(0.05)
        await runtime.runtime.complete_task(REVIEWER, task, {"accept": True, "note": "fine"})
        result = await run.wait(timeout=30)
    assert result.status == "completed", result.error
    assert result.output == {"reply": "Good news: your request is accepted."}

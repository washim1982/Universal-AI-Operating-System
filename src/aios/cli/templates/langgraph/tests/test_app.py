"""The LangGraph agent of Aios Template App answers, with a stand-in model."""

from pathlib import Path

from aios.testing.models import ScriptedModel
from aios.testing.runtime import LocalRuntime

APP = Path(__file__).parents[1]


async def test_it_answers() -> None:
    model = ScriptedModel("Hello from LangGraph!")
    async with LocalRuntime.from_app(APP, model=model) as runtime:
        result = await (await runtime.chat("aios-template-app", "Hello")).wait(timeout=30)
    assert result.status == "completed", result.error
    assert result.output["answer"] == "Hello from LangGraph!"
    [asked] = model.requests
    assert asked.system == "Answer briefly and accurately."

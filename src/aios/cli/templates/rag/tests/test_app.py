"""Aios Template App answers from its documents, with a stand-in model."""

from pathlib import Path

from aios.testing.models import ScriptedModel
from aios.testing.runtime import LocalRuntime

APP = Path(__file__).parents[1]


async def test_it_answers_from_its_documents() -> None:
    model = ScriptedModel({"answer": "Within 30 days of delivery."})
    async with LocalRuntime.from_app(APP, model=model) as runtime:
        await runtime.ingest_knowledge("aios-template-app", APP / "knowledge")
        result = await (
            await runtime.chat("aios-template-app", "How long do I have to return?")
        ).wait(timeout=30)
    assert result.status == "completed", result.error
    assert result.output["answer"] == "Within 30 days of delivery."
    assert result.output["sources"] == ["knowledge/returns.md"]
    [asked] = model.requests
    assert "30 days of delivery" in asked.messages[-1].content  # the document reached the model

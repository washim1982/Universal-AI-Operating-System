"""The AI Researcher's citations: every report source traces back to what research found."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from ai_researcher.agents import (
    Analysis,
    Finding,
    Report,
    ResearchResult,
    ResearchTask,
    Review,
    ReviewInput,
    Section,
    WriteInput,
    analyze,
    research,
    review,
    write,
)
from pydantic import BaseModel, JsonValue

from aios import Context

FOUND = "https://example.org/found-by-search"
INVENTED = "https://example.org/remembered-by-the-model"


class Model:
    def __init__(self, answer: BaseModel) -> None:
        self.answer = answer
        self.shown: list[str] = []
        self.system = ""

    async def generate(self, *, messages: list[str], system: str, **_: Any) -> BaseModel:
        self.shown += messages
        self.system = system
        return self.answer


def _context(model: Model) -> Context:
    event = SimpleNamespace(time=datetime(2026, 10, 30, tzinfo=UTC))
    return cast(Context, SimpleNamespace(model=model, user=str, event=event))


def _report(*cited: str) -> Report:
    sections = [Section(heading="H", text="T", sources=list(cited))]
    return Report(title="T", summary="S", sections=sections, sources=[INVENTED])


async def _researched(answer: ResearchResult, hits: JsonValue) -> ResearchResult:
    context = _context(Model(answer))

    async def invoke(*_: Any, **__: Any) -> JsonValue:
        return hits

    async def recall(*_: Any, **__: Any) -> str:
        return ""

    async def emit(*_: Any) -> None:
        return None

    stub = cast(Any, context)
    stub.tools = SimpleNamespace(invoke=invoke)
    stub.memory = SimpleNamespace(recall=recall)
    stub.emit = emit
    return await research(context, ResearchTask(question="Q"))


async def test_research_keeps_only_what_the_search_returned() -> None:
    answer = ResearchResult(question="Q", summary="S", sources=[FOUND, INVENTED])
    assert (await _researched(answer, [{"url": FOUND}])).sources == [FOUND]


async def test_research_citing_nothing_real_falls_back_to_the_search_results() -> None:
    answer = ResearchResult(question="Q", summary="S", sources=[INVENTED])
    assert (await _researched(answer, [{"url": FOUND}])).sources == [FOUND]


async def test_research_with_no_search_results_has_nothing_to_cite() -> None:
    answer = ResearchResult(question="Q", summary="S", sources=[INVENTED])
    assert (await _researched(answer, [])).sources == []


async def test_a_search_tool_with_another_answer_shape_leaves_nothing_to_cite() -> None:
    answer = ResearchResult(question="Q", summary="S", sources=[INVENTED])
    assert (await _researched(answer, "plain text answer")).sources == []


async def test_the_analysis_keeps_only_what_research_found() -> None:
    answer = Analysis(
        findings=[
            Finding(claim="Backed", sources=[FOUND, INVENTED]),
            Finding(claim="Made up", sources=[INVENTED]),
        ]
    )
    research = [ResearchResult(question="Q", summary="S", sources=[FOUND])]
    analysis = await analyze(_context(Model(answer)), research)
    assert analysis.findings == [Finding(claim="Backed", sources=[FOUND])]


async def test_with_nothing_to_cite_there_is_no_report() -> None:
    answer = Analysis(findings=[Finding(claim="Made up", sources=[INVENTED])])
    research_found = [ResearchResult(question="Q", summary="S", sources=[])]
    with pytest.raises(ValueError, match="nothing to cite"):
        await analyze(_context(Model(answer)), research_found)


async def test_a_draft_keeps_only_what_the_findings_hold() -> None:
    task = WriteInput(findings=[Finding(claim="C", sources=[FOUND])])
    report = await write(_context(Model(_report(FOUND, INVENTED))), task)
    assert report.sections[0].sources == [FOUND]
    assert report.sources == [FOUND]  # never empty: the schema needs one


async def test_a_section_left_without_sources_fails_the_review_without_a_model() -> None:
    model = Model(Review(result="approve"))
    task = ReviewInput(report=_report(), findings=[Finding(claim="C", sources=[FOUND])])
    verdict = await review(_context(model), task)
    assert verdict.result == "reject"
    assert model.shown == []


async def test_the_model_reviews_the_report_against_the_findings() -> None:
    model = Model(Review(result="approve"))
    task = ReviewInput(report=_report(FOUND), findings=[Finding(claim="C", sources=[FOUND])])
    assert (await review(_context(model), task)).result == "approve"
    [shown] = model.shown
    assert '"claim": "C"' in shown  # the evidence, beside the report
    assert '"title": "T"' in shown
    assert "Today is 2026-10-30." in model.system  # or it calls recent evidence impossible


async def test_a_revision_may_not_change_the_analysis_to_please_the_reviewer() -> None:
    model = Model(_report(FOUND))
    feedback = Review(result="reject", notes="2026 is in the future")
    task = WriteInput(findings=[Finding(claim="C", sources=[FOUND])], feedback=feedback)
    await write(_context(model), task)
    [shown] = model.shown
    assert "2026 is in the future" in shown
    assert "without changing any fact or date" in shown

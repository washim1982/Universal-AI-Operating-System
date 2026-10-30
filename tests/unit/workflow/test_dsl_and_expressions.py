from datetime import timedelta
from typing import Any

import pytest
import yaml

from aios.core.manifests import ApprovalStep, DynamicStep, TaskStep, ToolStep, load_manifests
from aios.sdk import Approval, Step, Workflow
from aios.workflow.expressions import ExpressionError, lookup, resolve


def _requirements_example() -> Workflow:
    """Requirements §7.6, verbatim apart from the publish tool's three-token name."""
    return (
        Workflow("research-report", version="1.0.0")
        .then(Step("plan", capability="planning"))
        .then(
            Step.dynamic(
                "research", from_plan="plan.output.tasks", capability="research.*", max_parallel=5
            )
        )
        .then(Step("analyze", capability="analysis"))
        .then(Step("draft", capability="writing"))
        .then(Step("review", capability="review", on_result={"reject": "draft"}, max_loops=2))
        .then(
            Approval(
                "publish-approval", approvers=["role:editor"], timeout="24h", on_timeout="deny"
            )
        )
        .then(Step("publish", tool="cms.page.publish", input={"title": "${draft.output.title}"}))
        .budget(max_cost_usd=3.00, max_duration="30m")
    )


def test_the_requirements_example_compiles() -> None:
    manifest = _requirements_example().compile()
    steps = manifest.spec.steps
    assert [type(s) for s in steps] == [
        TaskStep,
        DynamicStep,
        TaskStep,
        TaskStep,
        TaskStep,
        ApprovalStep,
        ToolStep,
    ]
    approval = steps[5]
    assert isinstance(approval, ApprovalStep)
    assert approval.timeout == timedelta(hours=24)
    assert manifest.spec.budget.max_duration == timedelta(minutes=30)
    assert manifest.spec.budget.max_cost_usd == 3.0


def test_to_yaml_round_trips_through_the_manifest_loader() -> None:
    workflow = _requirements_example()
    text = workflow.to_yaml()
    assert yaml.safe_load(text)["kind"] == "Workflow"
    [loaded] = load_manifests(text)
    assert loaded.model_dump() == workflow.compile().model_dump()


def test_steps_carry_needs_and_timeouts() -> None:
    compiled = (
        Workflow("fan-in")
        .then(Step("a", capability="x"))
        .then(Step("b", capability="x", needs=[], timeout="5m"))
        .then(Step("c", capability="x", needs=["a", "b"], max_attempts=1))
        .then(
            Step.dynamic("d", from_plan="c.output.items", capability="x", needs=["c"], timeout="1m")
        )
        .then(Approval("e", approvers=["user:ada"], needs=["d"]))
        .compile()
    )
    a, b, c, d, e = compiled.spec.steps
    assert (a.needs, b.needs, c.needs, d.needs, e.needs) == (None, [], ["a", "b"], ["c"], ["d"])
    assert b.timeout == timedelta(minutes=5)
    assert c.max_attempts == 1


@pytest.mark.parametrize(
    ("step", "message"),
    [
        (Step("x"), "exactly one of capability or tool"),
        (Step("x", capability="y", tool="a.b.c"), "exactly one of capability or tool"),
    ],
)
def test_invalid_steps(step: Step, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Workflow("w").then(step).compile()


def test_graph_errors_surface_at_compile_time() -> None:
    workflow = Workflow("w").then(Step("a", capability="x", input={"q": "${later.output}"}))
    with pytest.raises(ValueError, match="later"):
        workflow.compile()


OUTPUTS: dict[str, Any] = {"plan": {"tasks": [{"q": "a"}, {"q": "b"}], "title": "T"}}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("input", {"message": "hi"}),
        ("input.message", "hi"),
        ("plan.output", OUTPUTS["plan"]),
        ("plan.output.tasks.1.q", "b"),
    ],
)
def test_lookup(path: str, expected: Any) -> None:
    assert lookup(path, {"message": "hi"}, OUTPUTS) == expected


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("plan.tasks", "expected <step>.output"),
        ("missing.output", "no output yet"),
        ("plan.output.nope", "no 'nope'"),
        ("plan.output.tasks.9", "no '9'"),
    ],
)
def test_lookup_errors(path: str, message: str) -> None:
    with pytest.raises(ExpressionError, match=message):
        lookup(path, {}, OUTPUTS)


def test_resolve_keeps_types_and_substitutes_text() -> None:
    template = {
        "tasks": "${plan.output.tasks}",
        "headline": "About ${plan.output.title}: ${input.message}",
        "items": ["${plan.output.title}", 3],
        "count": 2,
        "json": "tasks=${plan.output.tasks}",
    }
    assert resolve(template, {"message": "hi"}, OUTPUTS) == {
        "tasks": [{"q": "a"}, {"q": "b"}],
        "headline": "About T: hi",
        "items": ["T", 3],
        "count": 2,
        "json": 'tasks=[{"q": "a"}, {"q": "b"}]',
    }

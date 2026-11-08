"""Scores, the gate and comparison (FR-OBS-07), and what an Evaluation manifest may say."""

from typing import Any

import pytest

from aios.core.evals import CaseResult, Experiment, Grade, case_score, check_gate, compare
from aios.core.manifests import (
    AppManifest,
    EvalGate,
    EvaluationManifest,
    load_evaluation,
)

SUITE = """
apiVersion: aios/v1alpha1
kind: Evaluation
metadata: {name: quality, version: 1.0.0}
spec:
  app: desk
  cases:
    - {id: one, input: {message: hi}}
  graders:
    - {name: polite, type: contains, value: hello}
"""


def _case(name: str, *scores: float | None, drifted: int = 0, **metrics: float) -> CaseResult:
    grades = [
        Grade(grader=f"g{i}", score=s, passed=None if s is None else s >= 0.5)
        for i, s in enumerate(scores)
    ]
    return CaseResult(
        case=name, status="completed", grades=grades, drifted=drifted, metrics=metrics
    )


def _experiment(*cases: CaseResult, status: Any = "completed") -> Experiment:
    return Experiment(experiment="01E", app="desk", status=status, cases=list(cases))


def test_a_case_scores_the_weighted_mean_of_what_was_given() -> None:
    grades = [
        Grade(grader="a", score=1.0, weight=3),
        Grade(grader="b", score=0.0, weight=1),
        Grade(grader="person"),  # pending: counts for nothing yet
    ]
    assert case_score(grades) == 0.75
    assert case_score([Grade(grader="person")]) is None


def test_an_experiment_scores_the_mean_of_its_cases() -> None:
    found = _experiment(_case("a", 1.0), _case("b", 0.5, 0.0), _case("c", None))
    assert found.score == pytest.approx(0.625)
    assert found.pending == 1
    summary = found.summary()
    assert (summary["passed"], summary["failed"], summary["pending"]) == (1, 1, 1)
    assert summary["metrics"] == {"g0": 0.75, "g1": 0.0}


def test_a_summary_reads_back_as_the_same_experiment() -> None:
    found = _experiment(_case("a", 1.0, 0.0, recall=0.5))
    assert Experiment.from_summary(found.summary()) == found


def test_the_gate_passes_what_meets_it() -> None:
    assert check_gate(_experiment(_case("a", 0.9)), EvalGate(min_score=0.8)).passed


@pytest.mark.parametrize(
    ("experiment", "gate", "baseline", "says"),
    [
        (_experiment(_case("a", 0.5)), EvalGate(min_score=0.8), None, "below the minimum"),
        (
            _experiment(_case("a", 0.7)),
            EvalGate(),
            _experiment(_case("a", 0.8)),
            "below the baseline",
        ),
        (
            _experiment(_case("a", 1.0, recall=0.5)),
            EvalGate(),
            _experiment(_case("a", 1.0, recall=1.0)),
            "recall fell",
        ),
        (_experiment(_case("a", 1.0, drifted=2)), EvalGate(), None, "record it again"),
        (_experiment(_case("a", 1.0, None)), EvalGate(), None, "waiting for a person"),
        (_experiment(_case("a", 1.0), status="running"), EvalGate(), None, "not completed"),
        (_experiment(), EvalGate(), None, "nothing was graded"),
    ],
)
def test_the_gate_blocks_and_says_why(
    experiment: Experiment, gate: EvalGate, baseline: Experiment | None, says: str
) -> None:
    found = check_gate(experiment, gate, baseline)
    assert not found.passed
    assert any(says in reason for reason in found.reasons), found.reasons


def test_the_gate_allows_what_it_is_told_to() -> None:
    lower = _experiment(_case("a", 0.75))
    assert check_gate(lower, EvalGate(max_regression=0.1), _experiment(_case("a", 0.8))).passed
    drifted = _experiment(_case("a", 1.0, drifted=5))
    assert check_gate(drifted, EvalGate(on_drift="warn")).passed
    # adding up to the same score isn't a regression
    same = _experiment(_case("a", 0.1 + 0.2))
    assert check_gate(same, EvalGate(), _experiment(_case("a", 0.3))).passed


def test_two_experiments_compared_case_by_case() -> None:
    a = _experiment(_case("one", 1.0), _case("two", 0.5))
    b = _experiment(_case("one", 0.5), _case("three", 1.0))
    found = compare(a, b).view()
    by_case = {case["case"]: case for case in found["cases"]}
    assert by_case["one"]["change"] == -0.5
    assert by_case["two"]["b"] is None
    assert by_case["three"]["a"] is None
    assert found["score"] == [0.75, 0.75]


def test_a_suite_loads() -> None:
    suite = load_evaluation(SUITE)
    assert suite.spec.cases[0].input == {"message": "hi"}
    assert suite.spec.gate == EvalGate()


@pytest.mark.parametrize(
    ("spec", "says"),
    [  # pyright: ignore[reportUnknownArgumentType] - suites of every shape
        (
            {
                "cases": [{"id": "a", "input": 1}, {"id": "a", "input": 2}],
                "graders": [{"name": "g", "type": "regex", "pattern": "x"}],
            },
            "share an id",
        ),
        (
            {
                "cases": [{"id": "a", "input": 1}],
                "graders": [
                    {"name": "g", "type": "regex", "pattern": "x"},
                    {"name": "g", "type": "regex", "pattern": "y"},
                ],
            },
            "share a name",
        ),
        ({"cases": [{"id": "a", "input": 1}]}, "needs graders"),
        (
            {"cases": [{"id": "a"}], "graders": [{"name": "g", "type": "regex", "pattern": "x"}]},
            "needs an input",
        ),
        ({"cases": [{"id": "a", "query": "q"}], "retrieval": {}}, "needs a query and relevant"),
        (
            {
                "cases": [{"id": "a", "query": "q", "relevant": ["x.md"]}],
                "retrieval": {},
                "graders": [{"name": "g", "type": "regex", "pattern": "x"}],
            },
            "leave graders out",
        ),
        (
            {
                "cases": [{"id": "a", "input": 1}],
                "graders": [{"name": "p", "type": "human", "reviewers": []}],
            },
            "at least 1",
        ),
    ],
)
def test_a_suite_that_doesnt_make_sense_is_refused(spec: dict[str, Any], says: str) -> None:
    with pytest.raises(ValueError, match=says):
        EvaluationManifest.model_validate(
            {"metadata": {"name": "s", "version": "1.0.0"}, "spec": spec}
        )


def _app(online: dict[str, Any]) -> AppManifest:
    return AppManifest.model_validate(
        {
            "metadata": {"name": "desk", "version": "1.0.0"},
            "spec": {
                "entrypoints": [{"type": "chat", "workflow": "w"}],
                "workflows": ["w"],
                "evaluation": {"online": online},
            },
        }
    )


def test_an_app_grades_a_sample_of_its_runs_online() -> None:
    app = _app({"sample": 0.1, "graders": [{"name": "polite", "type": "contains", "value": "hi"}]})
    assert app.spec.evaluation.online is not None
    assert app.spec.evaluation.online.sample == 0.1


@pytest.mark.parametrize(
    ("online", "says"),
    [
        (
            {"sample": 0.1, "graders": [{"name": "p", "type": "human", "reviewers": ["role:qa"]}]},
            "asks no person",
        ),
        ({"sample": 0.1, "graders": [{"name": "e", "type": "exact"}]}, "exact needs a value"),
        (
            {"sample": 0, "graders": [{"name": "c", "type": "contains", "value": "x"}]},
            "greater than 0",
        ),
    ],
)
def test_online_evaluation_needs_no_answer(online: dict[str, Any], says: str) -> None:
    with pytest.raises(ValueError, match=says):
        _app(online)

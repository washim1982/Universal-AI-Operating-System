"""Evaluation results (FR-OBS-07, FR-MEM-12): grades, cases, experiments, the gate, comparison.

An experiment is a sequence of ``eval.*`` events; :func:`experiment_of` folds them into what the
API shows and the gate checks. Scores run from 0 to 1. A case's score is the weighted mean of its
grades; an experiment's is the mean of its cases'. A grade a person still owes is pending: it
counts for nothing until it is given, and an experiment with one can't pass its gate.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aios.core.envelope import Event
from aios.core.manifests import EvalGate

TOLERANCE = 1e-9
"""Scores are compared with this much room, so 0.1 + 0.2 isn't a regression from 0.3."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Grade(_Frozen):
    grader: str
    score: float | None = Field(default=None, ge=0, le=1, description="None while pending")
    passed: bool | None = None
    reason: str = ""
    weight: float = 1.0
    by: str | None = Field(default=None, description="The person who gave it")

    @property
    def pending(self) -> bool:
        return self.score is None


class CaseResult(_Frozen):
    case: str
    run: str | None = None
    status: Literal["completed", "failed", "cancelled", "timeout", "error", "retrieved"]
    output: JsonValue = None
    error: str | None = None
    grades: list[Grade] = Field(default_factory=list[Grade])
    cost_usd: float = 0.0
    drifted: int = Field(default=0, description="Model calls played from a stale recording")
    metrics: dict[str, float] = Field(default_factory=dict[str, float])

    @property
    def score(self) -> float | None:
        return case_score(self.grades)

    @property
    def pending(self) -> int:
        return sum(grade.pending for grade in self.grades)

    def graded(self, grader: str, score: float, *, by: str, comment: str | None) -> CaseResult:
        """This case with a person's grade in place of the one it owed."""
        given = [
            grade.model_copy(
                update={"score": score, "passed": score >= 0.5, "by": by, "reason": comment or ""}
            )
            if grade.grader == grader and grade.pending
            else grade
            for grade in self.grades
        ]
        return self.model_copy(update={"grades": given})


class Experiment(_Frozen):
    experiment: str
    app: str
    suite: str = ""
    suite_version: str = ""
    app_version: str | None = None
    workflow_version: str | None = None
    label: str | None = None
    status: Literal["requested", "running", "completed"] = "requested"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    by: str | None = None
    gate: EvalGate = Field(default_factory=EvalGate)
    cases: list[CaseResult] = Field(default_factory=list[CaseResult])
    started_cases: dict[str, str | None] = Field(
        default_factory=dict[str, str | None], description="Case → its run, once begun"
    )

    @property
    def score(self) -> float | None:
        scores = [score for case in self.cases if (score := case.score) is not None]
        return sum(scores) / len(scores) if scores else None

    @property
    def pending(self) -> int:
        return sum(case.pending for case in self.cases)

    @property
    def drifted(self) -> int:
        return sum(case.drifted for case in self.cases)

    @property
    def cost_usd(self) -> float:
        return round(sum(case.cost_usd for case in self.cases), 8)

    @property
    def metrics(self) -> dict[str, float]:
        """Each grader's mean, and each retrieval metric's, over the cases that have it."""
        found: dict[str, list[float]] = {}
        for case in self.cases:
            for grade in case.grades:
                if grade.score is not None:
                    found.setdefault(grade.grader, []).append(grade.score)
            for name, value in case.metrics.items():
                found.setdefault(name, []).append(value)
        return {name: round(sum(v) / len(v), 6) for name, v in sorted(found.items())}

    @classmethod
    def from_summary(cls, summary: dict[str, Any]) -> Experiment:
        """An experiment from what :meth:`summary` wrote, such as a baseline file."""
        kept = {key: value for key, value in summary.items() if key not in _COMPUTED}
        kept["cases"] = [
            {key: value for key, value in case.items() if key != "score"}
            for case in cast(list[dict[str, Any]], summary.get("cases") or [])
        ]
        return cls.model_validate(kept)

    def summary(self) -> dict[str, Any]:
        """What the API and the CLI show, with the computed figures."""
        score = self.score
        return {
            **self.model_dump(mode="json", exclude={"started_cases"}),
            "score": None if score is None else round(score, 6),
            "passed": sum(1 for c in self.cases if (s := c.score) is not None and s >= 0.5),
            "failed": sum(1 for c in self.cases if (s := c.score) is not None and s < 0.5),
            "pending": self.pending,
            "drifted": self.drifted,
            "cost_usd": self.cost_usd,
            "metrics": self.metrics,
            "cases": [{**case.model_dump(mode="json"), "score": case.score} for case in self.cases],
        }


_COMPUTED = frozenset(
    {"score", "passed", "failed", "pending", "drifted", "cost_usd", "metrics", "gate_result"}
)


def case_score(grades: Sequence[Grade]) -> float | None:
    given = [grade for grade in grades if grade.score is not None]
    total = sum(grade.weight for grade in given)
    if not given or total <= 0:
        return None
    return sum(cast(float, grade.score) * grade.weight for grade in given) / total


def experiment_of(app: str, experiment: str, events: Iterable[Event]) -> Experiment | None:
    """An experiment from its events, in order; ``None`` if nothing was ever asked for."""
    found: Experiment | None = None
    for event in events:
        data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
        if event.type == "eval.experiment.requested":
            suite = cast(dict[str, Any], data.get("suite") or {})
            metadata = cast(dict[str, Any], suite.get("metadata") or {})
            spec = cast(dict[str, Any], suite.get("spec") or {})
            found = Experiment(
                experiment=experiment,
                app=app,
                suite=str(metadata.get("name", "")),
                suite_version=str(metadata.get("version", "")),
                label=data.get("label"),
                by=event.onbehalfof or event.principal,
                gate=EvalGate.model_validate(spec.get("gate") or {}),
            )
        elif found is None:
            continue
        elif event.type == "eval.experiment.started":
            found = found.model_copy(
                update={
                    "status": "running",
                    "started_at": event.time,
                    "app_version": data.get("app_version"),
                    "workflow_version": data.get("workflow_version"),
                }
            )
        elif event.type == "eval.case.started":
            begun = {**found.started_cases, str(data["case"]): data.get("run")}
            found = found.model_copy(update={"started_cases": begun})
        elif event.type == "eval.case.completed":
            result = CaseResult.model_validate(data["result"])
            kept = [case for case in found.cases if case.case != result.case]
            found = found.model_copy(update={"cases": [*kept, result]})
        elif event.type == "eval.case.graded":
            graded = [
                case.graded(
                    str(data["grader"]),
                    float(data["score"]),
                    by=event.principal,
                    comment=data.get("comment"),
                )
                if case.case == data["case"]
                else case
                for case in found.cases
            ]
            found = found.model_copy(update={"cases": graded})
        elif event.type == "eval.experiment.completed":
            found = found.model_copy(update={"status": "completed", "ended_at": event.time})
    return found


class GateResult(_Frozen):
    passed: bool
    reasons: list[str] = Field(default_factory=list[str])
    score: float | None = None
    baseline: float | None = None


def check_gate(
    experiment: Experiment, gate: EvalGate, baseline: Experiment | None = None
) -> GateResult:
    """Whether ``experiment`` may ship: CI fails, and a deploy stops, when it may not."""
    reasons: list[str] = []
    score = experiment.score
    if experiment.status != "completed":
        reasons.append(f"the experiment is {experiment.status}, not completed")
    if experiment.pending:
        owed = experiment.pending
        reasons.append(f"waiting for a person to give {owed} grade{'s' if owed > 1 else ''}")
    if score is None:
        reasons.append("nothing was graded")
    elif score + TOLERANCE < gate.min_score:
        reasons.append(f"score {score:.3f} is below the minimum {gate.min_score:.3f}")
    if experiment.drifted and gate.on_drift == "fail":
        reasons.append(
            f"{experiment.drifted} model calls were answered from a recording made for another "
            "request: record it again"
        )
    if baseline is not None:
        reasons += _regressions(experiment, baseline, gate.max_regression)
    return GateResult(
        passed=not reasons, reasons=reasons, score=score, baseline=baseline and baseline.score
    )


def _regressions(experiment: Experiment, baseline: Experiment, allowed: float) -> list[str]:
    found: list[str] = []
    now, then = experiment.score, baseline.score
    if now is not None and then is not None and then - now > allowed + TOLERANCE:
        found.append(
            f"score {now:.3f} is {then - now:.3f} below the baseline {then:.3f}, "
            f"more than the {allowed:.3f} allowed"
        )
    current = experiment.metrics
    for name, before in baseline.metrics.items():
        after = current.get(name)
        if after is not None and before - after > allowed + TOLERANCE:
            found.append(f"{name} fell from {before:.3f} to {after:.3f}")
    return found


class CaseComparison(_Frozen):
    case: str
    a: float | None
    b: float | None

    @property
    def change(self) -> float | None:
        return None if self.a is None or self.b is None else round(self.b - self.a, 6)


class ExperimentComparison(_Frozen):
    a: str
    b: str
    score: tuple[float | None, float | None]
    metrics: dict[str, tuple[float | None, float | None]]
    cases: list[CaseComparison]

    def view(self) -> dict[str, Any]:
        return {
            **self.model_dump(mode="json"),
            "cases": [{**case.model_dump(), "change": case.change} for case in self.cases],
        }


def compare(a: Experiment, b: Experiment) -> ExperimentComparison:
    """Two experiments case by case, usually one suite on two versions of an App."""
    scores_a = {case.case: case.score for case in a.cases}
    scores_b = {case.case: case.score for case in b.cases}
    order = list(scores_a) + [case for case in scores_b if case not in scores_a]
    metrics_a, metrics_b = a.metrics, b.metrics
    names = list(metrics_a) + [name for name in metrics_b if name not in metrics_a]
    return ExperimentComparison(
        a=a.experiment,
        b=b.experiment,
        score=(a.score, b.score),
        metrics={name: (metrics_a.get(name), metrics_b.get(name)) for name in names},
        cases=[CaseComparison(case=c, a=scores_a.get(c), b=scores_b.get(c)) for c in order],
    )

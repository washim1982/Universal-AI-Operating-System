"""The evaluator (FR-OBS-07, FR-MEM-12): experiments, people's grades, and online evaluation.

An experiment runs each case of a suite as an ordinary run of the App, waits for it, and grades
it. It is a sequence of ``eval.*`` events in the App's scope under the experiment's id, so a
worker that takes one over after a crash reads where it got to and starts no case twice.

Judges call models through the Model Kernel as the App's ``aios-evaluator`` agent: the App's model
policy, budgets and consent apply to them as to its own agents, and what they grade is quoted as
untrusted data. A retrieval suite searches the App's memory as that agent, so policy decides what
it may read.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, JsonValue

from aios.core.catalog import (
    EvalCaseCompleted,
    EvalCaseGraded,
    EvalCaseStarted,
    EvalExperimentCompleted,
    EvalExperimentRequested,
    EvalExperimentStarted,
    EvalRunGraded,
)
from aios.core.envelope import DataClass, Event
from aios.core.errors import (
    AIOSError,
    BusClosedError,
    ConflictError,
    NotFoundError,
    PolicyDeniedError,
)
from aios.core.evals import CaseResult, Experiment, Grade, experiment_of
from aios.core.human import selected
from aios.core.identity import Identity
from aios.core.ids import new_ulid
from aios.core.manifests import (
    EvalCase,
    EvaluationManifest,
    GroundedGrader,
    HumanGrader,
    JudgeGrader,
    OnlineEvaluation,
    RetrievalEval,
)
from aios.core.ports import CallSite
from aios.evals import (
    deterministic,
    grounded,
    groundedness,
    judge_request,
    judged,
    ranked_documents,
    recall_at,
    reciprocal_rank,
    strings,
)
from aios.fabric.port import Delivery, Scope, Subscription
from aios.workflow import RunState

if TYPE_CHECKING:
    from aios.controlplane.runtime import Runtime

logger = logging.getLogger("aios.evals")

EVALUATOR = "aios-evaluator"
"""The agent a judge, and a retrieval search, act as: one of the App's own, to policy."""
PRINCIPAL = "system:evaluator"
"""Who the evaluator's own events are by."""
CASE_TIMEOUT = 600.0
POLL = 0.25
ENDED = ("completed", "failed", "cancelled")

Extend = Callable[[], Awaitable[None]]


def sampled(run_id: str, share: float) -> bool:
    """Whether a run is in the sample. By its id, so every worker agrees and a retry doesn't
    change the answer."""
    digest = int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:8], "big")
    return digest / 2**64 < share


class Evaluator:
    def __init__(self, runtime: Runtime, *, lease: float = 30.0) -> None:
        self.runtime = runtime
        self.lease = lease
        self._subscriptions: list[Subscription] = []
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def bus(self) -> Any:
        return self.runtime.bus

    # ------------------------------------------------------------------ asking and reading

    async def request(
        self,
        tenant: str,
        app: str,
        suite: EvaluationManifest,
        *,
        principal: str,
        on_behalf_of: str | None,
        label: str | None = None,
    ) -> str:
        """Ask for ``suite`` to be run against ``app``; a worker runs it. Returns its id."""
        self.runtime.app(app, tenant=tenant)
        if suite.spec.app is not None and suite.spec.app != app:
            raise ConflictError(f"the suite is for {suite.spec.app}, not {app}")
        experiment = new_ulid()
        payload = EvalExperimentRequested(
            suite=suite.model_dump(mode="json", by_alias=True), label=label
        )
        await self.bus.publish(
            Event(
                source=f"aios://{tenant}/apps/{app}/eval",
                type="eval.experiment.requested",
                tenantid=tenant,
                appid=app,
                runid=experiment,
                correlationid=experiment,
                principal=principal,
                onbehalfof=on_behalf_of,
                data=payload.model_dump(mode="json"),
            )
        )
        return experiment

    async def _events(self, tenant: str, app: str, experiment: str) -> list[Event]:
        scope = Scope(tenant, app)
        return [s.event async for s in self.bus.replay(scope, "eval.>", run_id=experiment)]

    async def experiment(self, tenant: str, app: str, experiment: str) -> Experiment | None:
        return experiment_of(app, experiment, await self._events(tenant, app, experiment))

    async def experiments(self, tenant: str, app: str, limit: int = 50) -> list[Experiment]:
        """The App's experiments, newest first."""
        asked = [
            s.event.runid
            async for s in self.bus.replay(Scope(tenant, app), "eval.experiment.requested")
            if s.event.runid is not None
        ]
        found: list[Experiment] = []
        for experiment in reversed(asked[-limit:]):
            if (known := await self.experiment(tenant, app, experiment)) is not None:
                found.append(known)
        return found

    async def online(self, tenant: str, app: str, limit: int = 50) -> list[dict[str, JsonValue]]:
        """The latest production runs graded online, newest first."""
        graded = [s.event async for s in self.bus.replay(Scope(tenant, app), "eval.run.graded")]
        return [
            {"run": event.runid, "at": event.time.isoformat(), **cast(dict[str, Any], event.data)}
            for event in reversed(graded[-limit:])
        ]

    # ------------------------------------------------------------------ people's grades

    async def grade(
        self,
        by: Identity,
        app: str,
        experiment: str,
        *,
        case: str,
        grader: str,
        score: float,
        comment: str | None = None,
    ) -> Experiment:
        """A person's grade for a case that asked for one (FR-OBS-07 human graders)."""
        events = await self._events(by.tenant, app, experiment)
        known = experiment_of(app, experiment, events)
        if known is None:
            raise NotFoundError(f"no experiment {experiment}")
        suite = _suite(events[0])
        human = next(
            (g for g in suite.spec.graders if g.name == grader and isinstance(g, HumanGrader)),
            None,
        )
        result = next((c for c in known.cases if c.case == case), None)
        if human is None or result is None:
            raise NotFoundError(f"no grade {grader} for case {case} in experiment {experiment}")
        if not any(selected(selector, by) for selector in human.reviewers):
            raise PolicyDeniedError(f"grader {grader} isn't for this caller")
        if not any(g.grader == grader and g.pending for g in result.grades):
            raise ConflictError(f"case {case} was already graded by {grader}")
        started = next(e for e in events if e.type == "eval.experiment.requested")
        payload = EvalCaseGraded(case=case, grader=grader, score=score, comment=comment)
        await self.bus.publish(
            started.derive(
                "eval.case.graded",
                payload.model_dump(mode="json"),
                source=f"aios://{by.tenant}/apps/{app}/eval",
                principal=by.principal,
            )
        )
        found = await self.experiment(by.tenant, app, experiment)
        return cast(Experiment, found)

    # ------------------------------------------------------------------ running

    async def run(
        self, tenant: str, app: str, experiment: str, *, extend: Extend | None = None
    ) -> Experiment:
        """Run an experiment that was asked for, or carry on with one a crash interrupted."""
        events = await self._events(tenant, app, experiment)
        asked = next((e for e in events if e.type == "eval.experiment.requested"), None)
        if asked is None:
            raise NotFoundError(f"no experiment {experiment}")
        known = cast(Experiment, experiment_of(app, experiment, events))
        if known.status == "completed":
            return known
        suite = _suite(asked)
        installed = self.runtime.app(app, tenant=tenant)
        if known.status == "requested":
            entry = installed.manifest.spec.entrypoints[0].workflow
            workflow = installed.workflows.get(entry)
            started = EvalExperimentStarted(
                suite=suite.metadata.name,
                suite_version=suite.metadata.version,
                app_version=installed.manifest.metadata.version,
                workflow_version=workflow.metadata.version if workflow else None,
                label=known.label,
                cases=len(suite.spec.cases),
                gate=suite.spec.gate.model_dump(mode="json", by_alias=True),
            )
            await self._emit(asked, "eval.experiment.started", started)
        timeout = suite.spec.timeout.total_seconds() if suite.spec.timeout else CASE_TIMEOUT
        done = {case.case for case in known.cases}
        for case in suite.spec.cases:
            if case.id in done:
                continue
            if suite.spec.retrieval is not None:
                result = await self._retrieve(asked, case, suite.spec.retrieval)
            else:
                result = await self._case(
                    asked, suite, case, events=events, timeout=timeout, extend=extend
                )
            await self._emit(
                asked,
                "eval.case.completed",
                EvalCaseCompleted(case=case.id, result=result.model_dump(mode="json")),
            )
        finished = cast(Experiment, await self.experiment(tenant, app, experiment))
        summary = finished.summary()
        await self._emit(
            asked,
            "eval.experiment.completed",
            EvalExperimentCompleted(
                score=summary["score"],
                passed=summary["passed"],
                failed=summary["failed"],
                pending=summary["pending"],
                drifted=summary["drifted"],
                cost_usd=summary["cost_usd"],
                metrics=summary["metrics"],
            ),
        )
        return cast(Experiment, await self.experiment(tenant, app, experiment))

    async def _emit(self, cause: Event, type: str, payload: BaseModel) -> Event:
        event = cause.derive(
            type,
            payload.model_dump(mode="json"),
            source=f"aios://{cause.tenantid}/apps/{cause.appid}/eval",
            principal=PRINCIPAL,
            dataclass=cause.dataclass,
        )
        await self.bus.publish(event)
        return event

    async def _case(
        self,
        asked: Event,
        suite: EvaluationManifest,
        case: EvalCase,
        *,
        events: Sequence[Event],
        timeout: float,
        extend: Extend | None,
    ) -> CaseResult:
        tenant, app = asked.tenantid, asked.appid
        begun = next(
            (
                e
                for e in events
                if e.type == "eval.case.started"
                and isinstance(e.data, dict)
                and e.data.get("case") == case.id
            ),
            None,
        )
        if begun is None:  # not started before a crash: start it now
            run = await self.runtime.chat(
                app,
                input=case.input,
                tenant=tenant,
                principal=asked.onbehalfof or asked.principal,
            )
            begun = await self._emit(
                asked, "eval.case.started", EvalCaseStarted(case=case.id, run=run)
            )
        run = cast(dict[str, Any], begun.data)["run"]
        state = await self._finished(tenant, app, run, timeout, extend)
        if state is None:
            with contextlib.suppress(AIOSError):
                await self.runtime.cancel(app, run, tenant=tenant, principal=PRINCIPAL)
            return _failed(case, run, "timeout", f"no answer within {timeout:.0f} s", suite)
        ended = state.ended
        data: dict[str, Any] = (
            ended.data if ended is not None and isinstance(ended.data, dict) else {}
        )
        status = state.status
        if status != "completed":
            error = cast(dict[str, Any], data.get("error") or {})
            reason = f"the run {status}: {error.get('message', '')}".rstrip(": ")
            return _failed(case, run, status, reason, suite, cost=_cost(data))
        output = data.get("output")
        trail = [s.event async for s in self.bus.replay(Scope(tenant, app), run_id=run)]
        context = await self._context(tenant, trail)
        site_step = f"eval-{case.id}"
        grades: list[Grade] = []
        for grader in suite.spec.graders:
            if isinstance(grader, HumanGrader):
                grades.append(
                    Grade(grader=grader.name, reason=grader.question, weight=grader.weight)
                )
            elif isinstance(grader, JudgeGrader):
                grades.append(
                    await self._judge(
                        grader,
                        begun,
                        step=site_step,
                        asked=state.input,
                        output=output,
                        expected=case.expected,
                        data_class=state.data_class,
                    )
                )
            elif isinstance(grader, GroundedGrader):
                grades.append(grounded(grader, output, context))
            else:
                grades.append(deterministic(grader, output, case.expected))
        return CaseResult(
            case=case.id,
            run=run,
            status="completed",
            output=output,
            grades=grades,
            cost_usd=_cost(data),
            drifted=_drifted(trail),
        )

    async def _finished(
        self, tenant: str, app: str, run: str, timeout: float, extend: Extend | None
    ) -> RunState | None:
        """The run once it has ended, keeping the lease on the work meanwhile; ``None`` if it
        didn't end in time."""
        waited = 0.0
        while waited < timeout:
            state = await self.runtime.run_state(app, run, tenant=tenant)
            if state.finished:
                return state
            if extend is not None:
                await extend()
            await asyncio.sleep(POLL)
            waited += POLL
        return None

    async def _context(self, tenant: str, trail: Sequence[Event]) -> str:
        """What a run read: its tool outputs, and the memory it recalled."""
        parts: list[str] = []
        items: list[str] = []
        for event in trail:
            data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
            if event.type == "tool.call.completed":
                parts.append(strings(data.get("output")))
            elif event.type == "memory.recalled":
                items += [str(item) for item in cast(list[Any], data.get("items") or [])]
        reader = Identity(PRINCIPAL, tenant)
        for item_id in dict.fromkeys(items):
            if (item := await self.runtime.memory.get_as(reader, item_id)) is not None:
                parts.append(item.content)
        return "\n".join(parts)

    async def _judge(
        self,
        grader: JudgeGrader,
        cause: Event,
        *,
        step: str,
        asked: JsonValue,
        output: JsonValue,
        expected: JsonValue,
        data_class: DataClass,
    ) -> Grade:
        request = judge_request(grader, asked, output, expected).model_copy(
            update={"data_class": data_class}
        )
        site = CallSite(
            event=cause,
            agent=EVALUATOR,
            principal=f"agent:{EVALUATOR}@{cause.tenantid}",
            index=0,
            step=step,
            origin=f"{grader.name}/",
            data_class=data_class,
        )
        try:
            response = await self.runtime.models.generate(request, site)
        except AIOSError as exc:
            return Grade(
                grader=grader.name,
                score=0.0,
                passed=False,
                reason=f"the judge couldn't answer: {exc}",
                weight=grader.weight,
            )
        return judged(grader, response)

    async def _retrieve(self, asked: Event, case: EvalCase, retrieval: RetrievalEval) -> CaseResult:
        """A retrieval case: search the App's memory as the evaluator, and score what came back."""
        begun = await self._emit(asked, "eval.case.started", EvalCaseStarted(case=case.id))
        site = CallSite(
            event=begun,
            agent=EVALUATOR,
            principal=f"agent:{EVALUATOR}@{asked.tenantid}",
            index=0,
            step=f"eval-{case.id}",
            origin="retrieval/",
        )
        try:
            found = await self.runtime.memory.search(
                cast(str, case.query), scopes=retrieval.scopes, site=site, k=retrieval.k
            )
        except AIOSError as exc:
            return CaseResult(case=case.id, status="error", error=str(exc), grades=[])
        documents = ranked_documents(hit.item for hit in found.hits)
        k = retrieval.k
        recall = recall_at(case.relevant, documents, k)
        rank = reciprocal_rank(case.relevant, documents)
        grades = [
            Grade(grader=f"recall@{k}", score=recall, passed=recall >= 1.0),
            Grade(grader="mrr", score=rank, passed=rank > 0),
        ]
        metrics = {f"recall@{k}": recall, "mrr": rank}
        if case.answer is not None:
            context = "\n".join(hit.item.content for hit in found.hits)
            score, reason = groundedness(case.answer, context)
            grades.append(
                Grade(grader="groundedness", score=score, passed=score >= 0.7, reason=reason)
            )
            metrics["groundedness"] = score
        return CaseResult(
            case=case.id,
            status="retrieved",
            output=cast(JsonValue, documents),
            grades=grades,
            metrics=metrics,
        )

    # ------------------------------------------------------------------ online

    async def _on_run(self, event: Event) -> None:
        """Grade a finished production run, if it is in its App's sample (FR-OBS-07)."""
        if event.type != "run.completed" or event.runid is None:
            return
        installed = self.runtime.apps.get((event.tenantid, event.appid))
        online: OnlineEvaluation | None = (
            installed.manifest.spec.evaluation.online if installed else None
        )
        if online is None or not sampled(event.runid, online.sample):
            return
        scope = Scope(event.tenantid, event.appid)
        async for _ in self.bus.replay(scope, "eval.run.graded", run_id=event.runid):
            return  # graded already: this is a redelivery
        state = await self.runtime.run_state(event.appid, event.runid, tenant=event.tenantid)
        data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
        output = data.get("output")
        trail = [s.event async for s in self.bus.replay(scope, run_id=event.runid)]
        context = await self._context(event.tenantid, trail)
        grades: list[Grade] = []
        for grader in online.graders:
            if isinstance(grader, JudgeGrader):
                grades.append(
                    await self._judge(
                        grader,
                        event,
                        step="online",
                        asked=state.input,
                        output=output,
                        expected=None,
                        data_class=state.data_class,
                    )
                )
            elif isinstance(grader, GroundedGrader):
                grades.append(grounded(grader, output, context))
            elif not isinstance(grader, HumanGrader):
                grades.append(deterministic(grader, output))
        result = CaseResult(case="online", run=event.runid, status="completed", grades=grades)
        payload = EvalRunGraded(
            score=result.score, grades=[g.model_dump(mode="json") for g in grades]
        )
        await self._emit(event, "eval.run.graded", payload)

    # ------------------------------------------------------------------ the worker's side

    async def start(self) -> None:
        await self._consume("eval.experiment.requested", "eval.experiments", self._on_request)
        await self._consume("run.completed", "eval.online", self._on_completed)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for subscription in self._subscriptions:
            await subscription.close()
        self._subscriptions.clear()

    async def _consume(
        self, pattern: str, group: str, handler: Callable[[Delivery], Awaitable[None]]
    ) -> None:
        subscription = await self.bus.subscribe(
            Scope.platform(), pattern, group=group, ack_wait=self.lease
        )
        self._subscriptions.append(subscription)
        self._tasks.append(asyncio.create_task(self._deliveries(subscription, handler)))

    async def _deliveries(
        self, subscription: Subscription, handler: Callable[[Delivery], Awaitable[None]]
    ) -> None:
        with contextlib.suppress(BusClosedError):
            async for delivery in subscription:
                try:
                    await handler(delivery)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("%s failed on %s: %s", delivery.group, delivery.event.id, exc)
                    await delivery.nack(reason=f"{delivery.group}: {type(exc).__name__}")
                    continue
                await delivery.ack()

    async def _on_request(self, delivery: Delivery) -> None:
        event = delivery.event
        if event.runid is None:
            return
        await self.run(event.tenantid, event.appid, event.runid, extend=delivery.extend)

    async def _on_completed(self, delivery: Delivery) -> None:
        await self._on_run(delivery.event)


def _suite(asked: Event) -> EvaluationManifest:
    data = cast(dict[str, Any], asked.data)
    return EvaluationManifest.model_validate(data["suite"])


def _cost(data: dict[str, Any]) -> float:
    usage = cast(dict[str, Any], data.get("usage") or {})
    return float(usage.get("cost_usd", 0.0))


def _drifted(trail: Sequence[Event]) -> int:
    return sum(
        1
        for event in trail
        if event.type == "model.call.completed"
        and isinstance(event.data, dict)
        and event.data.get("drifted")
    )


def _failed(
    case: EvalCase,
    run: str | None,
    status: Any,
    reason: str,
    suite: EvaluationManifest,
    *,
    cost: float = 0.0,
) -> CaseResult:
    """A case whose run didn't complete: every grade is 0, because there is nothing to grade."""
    grades = [
        Grade(grader=g.name, score=0.0, passed=False, reason=reason, weight=g.weight)
        for g in suite.spec.graders
    ]
    return CaseResult(
        case=case.id, run=run, status=status, error=reason, grades=grades, cost_usd=cost
    )

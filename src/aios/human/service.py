"""Human OS: approvals (WP 2.4; FR-HUM-01 to 05, 07, 10; ADR-016).

* **An approval is for one call.** Its id comes from the task, the place in the task and the exact
  arguments, so a yes for $120 can't be spent on $5,000, or on the same refund in another run.
* **Nothing waits in memory.** A task that needs an approval stops; the run keeps only its log.
  Once someone answers, the task runs again from the top and its recorded calls replay, so it
  reaches the same line, gets the answer there, and has repeated nothing (FR-HUM-10).
* **The first answer wins.** Decisions, timeouts and cancellations all close the same row in one
  transaction with their event, so only one of them can reach the run.
* **Silence means no.** An approval that nobody answers is denied, unless the rule or workflow step
  that asked for it says otherwise (FR-HUM-05).
* **Whoever an action is for can't approve it** (FR-HUM-04). Neither can anyone a policy didn't
  name: being a tenant admin is not enough unless the rule asks for tenant admins.

This module publishes ``approval.granted``, ``denied``, ``expired`` and ``cancelled``. Nothing else
does: agents can't publish in the ``approval`` domain, and the engine only listens.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

import jsonschema
from pydantic import BaseModel, JsonValue

from aios.core.cassettes import request_hash
from aios.core.catalog import (
    ApprovalCancelled,
    ApprovalDecided,
    ApprovalEscalated,
    ApprovalEscalation,
    ApprovalExpired,
    ApprovalModified,
    ApprovalReminded,
    ApprovalRequested,
    ApprovalVoted,
    HumanTaskAssigned,
    HumanTaskCancelled,
    HumanTaskCompleted,
    TaskClaimed,
    TaskCompleted,
)
from aios.core.envelope import Event
from aios.core.errors import (
    ApprovalClosedError,
    ApprovalDeniedError,
    ApprovalExpiredError,
    ApprovalForbiddenError,
    ApprovalRequiredError,
    AwaitingApproval,
    BusClosedError,
    ConflictError,
    NotFoundError,
    PolicyDeniedError,
)
from aios.core.human import (
    DEFAULT_APPROVAL_TIMEOUT,
    Approval,
    ApprovalStatus,
    Groups,
    HumanTask,
    Vote,
    selected,
)
from aios.core.identity import Identity
from aios.core.ids import ulid_for, ulid_time
from aios.core.manifests import HumanCapability
from aios.core.ports import ApprovalGateway, Approved, CallSite, Proposal
from aios.fabric.port import Delivery, EventBus, Scope, Subscription
from aios.human.channels import ApprovalNotifier
from aios.human.store import ApprovalStore
from aios.human.tasks import TaskStore
from aios.human.webhook import ApprovalWebhook

logger = logging.getLogger("aios.human")

PRINCIPAL = "system:human"
SOURCE = "aios://{tenant}/system/human"
SWEEP_SECONDS = 5.0
RUN_ENDED = ("run.completed", "run.failed", "run.cancelled")
ANSWERS = ("approval.granted", "approval.denied", "approval.expired", "approval.cancelled")
"""What settles an approval. A vote, a reminder or an escalation doesn't."""

GroupsOf = Callable[[str], Awaitable[Groups]]
HumansOf = Callable[[str, str, str], HumanCapability | None]
"""Who does a capability of an App of a tenant, if people do."""
WORKER = "human-os"


class HumanService(ApprovalGateway):
    def __init__(
        self,
        bus: EventBus,
        store: ApprovalStore,
        *,
        webhook: ApprovalWebhook | None = None,
        lease: float = 30.0,
        sweep: float = SWEEP_SECONDS,
        groups: GroupsOf | None = None,
        notifier: ApprovalNotifier | None = None,
        tasks: TaskStore | None = None,
        humans: HumansOf | None = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self.webhook = webhook
        self.notifier = notifier
        self.tasks = tasks
        self.humans = humans
        self.lease = lease
        self.sweep = sweep
        self.groups_of = groups
        self._subscriptions: list[Subscription] = []
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ the worker's side

    async def start(self) -> None:
        await self._consume("approval.requested", "human.requests", self._on_request)
        await self._consume("run.*", "human.runs", self._on_run)
        if self.webhook is not None:
            await self._consume("approval.*", "human.webhook", self.webhook.send)
        if self.notifier is not None:
            await self._consume("approval.*", "human.notify", self.notifier.handle)
        if self.tasks is not None and self.humans is not None:
            await self._consume("task.requested.>", "human.tasks", self._on_task)
        self._tasks.append(asyncio.create_task(self._sweeping()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for subscription in self._subscriptions:
            await subscription.close()
        self._subscriptions.clear()

    async def _consume(
        self, pattern: str, group: str, handler: Callable[[Event], Awaitable[None]]
    ) -> None:
        subscription = await self.bus.subscribe(
            Scope.platform(), pattern, group=group, ack_wait=self.lease
        )
        self._subscriptions.append(subscription)
        self._tasks.append(asyncio.create_task(self._deliveries(subscription, handler)))

    async def _deliveries(
        self, subscription: Subscription, handler: Callable[[Event], Awaitable[None]]
    ) -> None:
        with contextlib.suppress(BusClosedError):
            async for delivery in subscription:
                await self._handle(delivery, handler)

    async def _handle(
        self, delivery: Delivery, handler: Callable[[Event], Awaitable[None]]
    ) -> None:
        try:
            await handler(delivery.event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("%s failed on %s: %s", delivery.group, delivery.event.id, exc)
            await delivery.nack(reason=f"{delivery.group}: {type(exc).__name__}")
            return
        await delivery.ack()

    async def _on_request(self, event: Event) -> None:
        approval = Approval.of(event)
        await self.store.add(approval)
        if event.runid is not None and await self._ended(event):
            await self._cancel(approval, "the run had already ended")

    async def _ended(self, event: Event) -> bool:
        """The run's end can reach ``_on_run`` first: the two are separate consumer groups."""
        scope = Scope(event.tenantid, event.appid)
        async for stored in self.bus.replay(scope, "run.*", run_id=event.runid):
            if stored.event.type in RUN_ENDED:
                return True
        return False

    async def _on_run(self, event: Event) -> None:
        if event.type in RUN_ENDED and event.runid is not None:
            for approval in await self.store.in_run(event.tenantid, event.runid):
                await self._cancel(approval, event.type)
            if self.tasks is not None:
                for task in await self.tasks.in_run(event.tenantid, event.runid):
                    await self._close_task(
                        task, "cancelled", HumanTaskCancelled(task=task.id, reason=event.type)
                    )

    # ------------------------------------------------------------------ work for people

    async def _on_task(self, event: Event) -> None:
        """A task for a capability people provide reaches them as it would reach an agent."""
        if self.tasks is None or self.humans is None:
            return
        data = cast(dict[str, Any], event.data) if isinstance(event.data, dict) else {}
        capability = str(data.get("capability"))
        serving = self.humans(event.tenantid, event.appid, capability)
        if serving is None:
            return  # an agent's
        assigned = HumanTaskAssigned(
            task=event.id,
            capability=capability,
            assignees=serving.assignees,
            description=serving.description,
            input=data.get("input"),
            output_schema=serving.output,
            step=data.get("step"),
            item=data.get("item"),
        )
        source = SOURCE.format(tenant=event.tenantid)
        claimed = TaskClaimed(capability=capability, agent="people", instance=WORKER)
        events = [
            event.derive("task.claimed", claimed.model_dump(mode="json"), source=source,
                         principal=PRINCIPAL),
            event.derive("human.task.assigned", assigned.model_dump(mode="json"), source=source,
                         principal=PRINCIPAL),
        ]  # fmt: skip
        await self.tasks.add(HumanTask(event, assigned), events=events)

    async def assigned(self, by: Identity, *, everyone: bool = False) -> list[HumanTask]:
        """Open work ``by`` can do; with ``everyone``, all of the tenant's."""
        if self.tasks is None:
            return []
        groups = await self.groups(by.tenant)
        found = await self.tasks.pending(by.tenant)
        return found if everyone else [t for t in found if t.may_do(by, groups)]

    async def task(self, tenant: str, task_id: str) -> HumanTask | None:
        return None if self.tasks is None else await self.tasks.get(tenant, task_id)

    async def complete(
        self,
        by: Identity,
        task_id: str,
        output: JsonValue,
        *,
        comment: str | None = None,
        anyone: bool = False,
    ) -> HumanTask:
        """Do the work: ``output`` goes back to the run as the task's result. ``anyone`` is for
        a local server, which has no identities. Raises ``ValueError`` for an answer that
        doesn't match the capability's schema."""
        task = await self.task(by.tenant, task_id)
        if task is None:
            raise NotFoundError(f"no task {task_id}")
        if task.status != "pending":
            raise ConflictError(f"task {task_id} is already {task.status}")
        if not anyone and not task.may_do(by, await self.groups(by.tenant)):
            raise PolicyDeniedError(f"task {task_id} isn't for this caller")
        schema = task.details.output_schema
        if schema is not None:
            try:
                jsonschema.validate(output, schema)
            except jsonschema.ValidationError as exc:
                raise ValueError(f"the answer doesn't fit: {exc.message}") from exc
        payload = HumanTaskCompleted(task=task.id, by=by.principal, output=output, comment=comment)
        closed = await self._close_task(task, "completed", payload, by=by.principal)
        if closed is None:
            raise ConflictError(f"task {task_id} was closed a moment ago")
        return closed

    async def _close_task(
        self,
        task: HumanTask,
        status: Literal["completed", "cancelled"],
        payload: HumanTaskCompleted | HumanTaskCancelled,
        *,
        by: str = PRINCIPAL,
    ) -> HumanTask | None:
        if self.tasks is None:
            return None
        source = SOURCE.format(tenant=task.tenant)
        events = [
            task.request.derive(
                f"human.task.{status}", payload.model_dump(mode="json"), source=source, principal=by
            )
        ]
        output = payload.output if isinstance(payload, HumanTaskCompleted) else None
        if isinstance(payload, HumanTaskCompleted):
            result = TaskCompleted(capability=task.details.capability, output=output)
            events.append(
                task.request.derive(
                    "task.completed", result.model_dump(mode="json"), source=source, principal=by
                )
            )
        comment = payload.comment if isinstance(payload, HumanTaskCompleted) else None
        return await self.tasks.close(
            task,
            status=status,
            by=by,
            output=output,
            comment=comment,
            at=datetime.now(UTC),
            events=events,
        )

    async def _cancel(self, approval: Approval, reason: str) -> None:
        payload = ApprovalCancelled(approval=approval.id, reason=reason)
        await self._close(approval, "cancelled", payload, by=PRINCIPAL)

    async def _sweeping(self) -> None:
        while True:
            await asyncio.sleep(self.sweep)
            try:
                await self.expire()
                await self.nudge()
            except Exception:
                logger.exception("could not expire approvals; will try again")

    async def expire(self, now: datetime | None = None) -> int:
        """Close every approval past its time, each as its rule or step said to."""
        overdue = await self.store.overdue(now or datetime.now(UTC))
        for approval in overdue:
            approved = approval.details.on_timeout == "approve"
            payload = ApprovalExpired(approval=approval.id, approved=approved)
            await self._close(approval, "expired", payload, by=PRINCIPAL, approved=approved)
        return len(overdue)

    async def nudge(self, now: datetime | None = None) -> int:
        """Escalate what has waited too long, and remind whoever it waits for (FR-HUM-05)."""
        now = now or datetime.now(UTC)
        acted = 0
        for approval in await self.store.waiting():
            steps = approval.details.escalation
            level = sum(1 for step in steps if approval.request.time + step.after <= now)
            if level > approval.level:
                added = [a for step in steps[approval.level : level] for a in step.approvers]
                payload = ApprovalEscalated(approval=approval.id, level=level, approvers=added)
                changed = replace(approval, level=level, reminded_at=now)
                acted += await self._advance(approval, changed, "approval.escalated", payload)
                continue  # being asked is reminder enough
            every = approval.details.remind_every
            if every is not None and now - (approval.reminded_at or approval.request.time) >= every:
                count = approval.reminders + 1
                payload = ApprovalReminded(approval=approval.id, count=count)
                changed = replace(approval, reminders=count, reminded_at=now)
                acted += await self._advance(approval, changed, "approval.reminded", payload)
        return acted

    async def _advance(
        self, approval: Approval, changed: Approval, type: str, payload: BaseModel
    ) -> int:
        event = approval.request.derive(
            type,
            payload.model_dump(mode="json"),
            source=SOURCE.format(tenant=approval.tenant),
            principal=PRINCIPAL,
        )
        return int(await self.store.advance(approval, changed, event=event) is not None)

    async def _close(
        self,
        approval: Approval,
        status: ApprovalStatus,
        payload: BaseModel,
        *,
        by: str,
        approved: bool = False,
        comment: str | None = None,
        edits: dict[str, JsonValue] | None = None,
    ) -> Approval | None:
        event = approval.request.derive(
            f"approval.{status}",
            payload.model_dump(mode="json"),
            source=SOURCE.format(tenant=approval.tenant),
            principal=by,
        )
        return await self.store.settle(
            approval,
            status=status,
            approved=approved,
            by=by,
            comment=comment,
            at=datetime.now(UTC),
            event=event,
            edits=edits,
        )

    # ------------------------------------------------------------------ an agent's side

    async def require(self, proposal: Proposal, site: CallSite) -> Approved:
        event = site.event
        if event.type != "task.requested" or event.runid is None or site.step is None:
            raise ApprovalRequiredError(  # a subscription, say, has no step to pause
                f"{proposal.action}: approval required, and only a workflow step can wait for one",
                proposal.approvers,
            )
        approval = _approval_id(proposal, site)
        asked = False
        scope = Scope(event.tenantid, event.appid)
        async for stored in self.bus.replay(scope, "approval.*", run_id=event.runid):
            data = stored.event.data
            if not isinstance(data, dict) or data.get("approval") != approval:
                continue
            if stored.event.type == "approval.requested":
                asked = True
            elif stored.event.type in ANSWERS:
                return _answer(stored.event, approval, proposal)
        if not asked:
            await self._ask(approval, proposal, site)
        raise AwaitingApproval(approval)

    async def _ask(self, approval: str, proposal: Proposal, site: CallSite) -> None:
        timeout = proposal.timeout or DEFAULT_APPROVAL_TIMEOUT
        payload = ApprovalRequested(
            approval=approval,
            approvers=list(proposal.approvers),
            reason="a policy asks for approval",
            summary=proposal.summary,
            action=proposal.action,
            payload=proposal.payload,
            risk=proposal.risk,
            policies=list(proposal.policies),
            rationale=proposal.rationale,
            tainted=proposal.tainted,
            taint=list(proposal.taint),
            step=site.step,
            item=site.item,
            expires_at=datetime.now(UTC) + timeout,
            on_timeout=proposal.on_timeout,
            quorum=proposal.terms.quorum,
            remind_every=proposal.terms.remind_every,
            preview=proposal.preview,
            escalation=[
                ApprovalEscalation(after=step.after, approvers=list(step.approvers))
                for step in proposal.terms.escalation
            ],
        )
        await self.bus.publish(
            site.event.derive(
                "approval.requested",
                payload.model_dump(mode="json"),
                source=SOURCE.format(tenant=site.event.tenantid),
                principal=site.principal,
                dataclass=site.data_class,
                tainted=site.tainted,
            )
        )

    # ------------------------------------------------------------------ a person's side

    async def get(self, tenant: str, approval_id: str) -> Approval | None:
        return await self.store.get(tenant, approval_id)

    async def groups(self, tenant: str) -> Groups:
        return await self.groups_of(tenant) if self.groups_of is not None else {}

    async def inbox(self, by: Identity) -> list[Approval]:
        """What this caller can decide now."""
        groups = await self.groups(by.tenant)
        pending = await self.store.find(by.tenant, "pending")
        return [a for a in pending if a.may_decide(by, groups)]

    async def find(self, tenant: str, status: ApprovalStatus) -> list[Approval]:
        return await self.store.find(tenant, status)

    async def decide(
        self,
        by: Identity,
        approval_id: str,
        *,
        approve: bool,
        comment: str | None = None,
        any_approver: bool = False,
        edits: dict[str, JsonValue] | None = None,
        via: str | None = None,
    ) -> Approval:
        """Answer an approval. ``any_approver`` is for a local server, which has no identities.
        ``via`` names the channel whose signed link the answer came by (FR-HUM-07).

        ``edits`` changes some of what is approved (FR-HUM-06). The changed action is checked
        against policy again before it runs, and it may not need an approval this one doesn't give.
        """
        approval = await self.store.get(by.tenant, approval_id)
        if approval is None:
            raise NotFoundError(f"no approval {approval_id}")
        if approval.status != "pending":
            raise ApprovalClosedError(f"approval {approval_id} is already {approval.status}")
        if datetime.now(UTC) >= approval.expires_at:
            raise ApprovalClosedError(f"approval {approval_id} has expired")
        if by.principal == approval.initiator:
            raise ApprovalForbiddenError("whoever an action is for can't approve it")
        groups = await self.groups(by.tenant)
        if not any_approver and not any(selected(s, by, groups) for s in approval.approvers):
            raise ApprovalForbiddenError(f"approval {approval_id} isn't for this caller")
        if by.principal in approval.voted:
            raise ApprovalClosedError(f"you have approved {approval_id} already")
        changed = _edited(approval, edits, approve=approve)
        if approve and approval.quorum > 1:
            approval = await self._vote(approval, by, comment, via)
            if len(approval.votes) < approval.quorum:
                return approval
        voters = [*approval.voted] if approve else []
        payload = ApprovalDecided(
            approval=approval.id,
            by=by.principal,
            comment=comment,
            voters=voters,
            edits=edits if changed is not None else None,
            payload=changed,
            via=via,
        )
        status: ApprovalStatus = "granted" if approve else "denied"
        settled = await self._close(
            approval,
            status,
            payload,
            by=by.principal,
            approved=approve,
            comment=comment,
            edits=payload.edits,
        )
        if settled is None:
            raise ApprovalClosedError(f"approval {approval_id} was closed a moment ago")
        if changed is not None and edits is not None:
            modified = ApprovalModified(
                approval=approval.id, by=by.principal, edits=edits, payload=changed
            )
            await self.bus.publish(
                approval.request.derive(
                    "approval.modified",
                    modified.model_dump(mode="json"),
                    source=SOURCE.format(tenant=approval.tenant),
                    principal=by.principal,
                )
            )
        return settled

    async def _vote(
        self, approval: Approval, by: Identity, comment: str | None, via: str | None
    ) -> Approval:
        """One yes towards a quorum. Two people voting at once both count; one person, once."""
        payload = ApprovalVoted(
            approval=approval.id,
            by=by.principal,
            comment=comment,
            votes=len(approval.votes) + 1,
            quorum=approval.quorum,
            via=via,
        )
        event = approval.request.derive(
            "approval.voted",
            payload.model_dump(mode="json"),
            source=SOURCE.format(tenant=approval.tenant),
            principal=by.principal,
        )
        vote = Vote(by.principal, datetime.now(UTC), comment)
        voted = await self.store.vote(approval, vote, event=event)
        if voted is None:
            raise ApprovalClosedError(f"approval {approval.id} closed, or you have voted already")
        return voted


def _approval_id(proposal: Proposal, site: CallSite) -> str:
    """The same for one call however often its task runs, and for nothing else."""
    action = request_hash({"action": proposal.action, "payload": proposal.payload})
    return ulid_for(f"{site.task}/{site.key('tool')}/{action}", ulid_time(site.task))


def _edited(
    approval: Approval, edits: dict[str, JsonValue] | None, *, approve: bool
) -> dict[str, JsonValue] | None:
    """The action as an approver changed it; ``None`` without edits. Only a yes can change it, and
    only the parameters it has. With a quorum, everyone must have agreed to the same thing."""
    if edits is None:
        return None
    if not approve:
        raise ApprovalForbiddenError("only a yes can change what is approved")
    if approval.quorum > 1:
        raise ApprovalForbiddenError("with a quorum, approve it as asked, or deny it")
    payload = approval.details.payload
    if not isinstance(payload, dict):
        raise ValueError("only an action with named parameters can be changed")
    unknown = sorted(set(edits) - set(payload))
    if unknown:
        raise ValueError(f"the action has no {', '.join(unknown)}")
    return {**payload, **edits}


def _answer(event: Event, approval: str, proposal: Proposal) -> Approved:
    data: dict[str, Any] = cast(dict[str, Any], event.data) if isinstance(event.data, dict) else {}
    if event.type == "approval.granted":
        return Approved(approval, data.get("payload"))
    if event.type == "approval.expired" and data["approved"]:
        return Approved(approval)
    if event.type == "approval.expired":
        raise ApprovalExpiredError(f"{proposal.action}: nobody approved it in time")
    raise ApprovalDeniedError(f"{proposal.action}: {event.type.split('.')[1]} by {event.principal}")

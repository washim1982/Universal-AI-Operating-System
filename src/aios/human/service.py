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
from datetime import UTC, datetime

from pydantic import BaseModel

from aios.core.cassettes import request_hash
from aios.core.catalog import (
    ApprovalCancelled,
    ApprovalDecided,
    ApprovalExpired,
    ApprovalRequested,
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
    NotFoundError,
)
from aios.core.human import DEFAULT_APPROVAL_TIMEOUT, Approval, ApprovalStatus, selected
from aios.core.identity import Identity
from aios.core.ids import ulid_for, ulid_time
from aios.core.ports import ApprovalGateway, CallSite, Proposal
from aios.fabric.port import Delivery, EventBus, Scope, Subscription
from aios.human.store import ApprovalStore
from aios.human.webhook import ApprovalWebhook

logger = logging.getLogger("aios.human")

PRINCIPAL = "system:human"
SOURCE = "aios://{tenant}/system/human"
SWEEP_SECONDS = 5.0
RUN_ENDED = ("run.completed", "run.failed", "run.cancelled")


class HumanService(ApprovalGateway):
    def __init__(
        self,
        bus: EventBus,
        store: ApprovalStore,
        *,
        webhook: ApprovalWebhook | None = None,
        lease: float = 30.0,
        sweep: float = SWEEP_SECONDS,
    ) -> None:
        self.bus = bus
        self.store = store
        self.webhook = webhook
        self.lease = lease
        self.sweep = sweep
        self._subscriptions: list[Subscription] = []
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ the worker's side

    async def start(self) -> None:
        await self._consume("approval.requested", "human.requests", self._on_request)
        await self._consume("run.*", "human.runs", self._on_run)
        if self.webhook is not None:
            await self._consume("approval.*", "human.webhook", self.webhook.send)
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

    async def _cancel(self, approval: Approval, reason: str) -> None:
        payload = ApprovalCancelled(approval=approval.id, reason=reason)
        await self._close(approval, "cancelled", payload, by=PRINCIPAL)

    async def _sweeping(self) -> None:
        while True:
            await asyncio.sleep(self.sweep)
            try:
                await self.expire()
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

    async def _close(
        self,
        approval: Approval,
        status: ApprovalStatus,
        payload: BaseModel,
        *,
        by: str,
        approved: bool = False,
        comment: str | None = None,
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
        )

    # ------------------------------------------------------------------ an agent's side

    async def require(self, proposal: Proposal, site: CallSite) -> str:
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
            else:
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

    async def inbox(self, by: Identity) -> list[Approval]:
        """What this caller can decide now."""
        return [a for a in await self.store.find(by.tenant, "pending") if a.may_decide(by)]

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
    ) -> Approval:
        """Answer an approval. ``any_approver`` is for a local server, which has no identities."""
        approval = await self.store.get(by.tenant, approval_id)
        if approval is None:
            raise NotFoundError(f"no approval {approval_id}")
        if approval.status != "pending":
            raise ApprovalClosedError(f"approval {approval_id} is already {approval.status}")
        if datetime.now(UTC) >= approval.expires_at:
            raise ApprovalClosedError(f"approval {approval_id} has expired")
        if by.principal == approval.initiator:
            raise ApprovalForbiddenError("whoever an action is for can't approve it")
        if not any_approver and not any(selected(s, by) for s in approval.details.approvers):
            raise ApprovalForbiddenError(f"approval {approval_id} isn't for this caller")
        payload = ApprovalDecided(approval=approval.id, by=by.principal, comment=comment)
        status: ApprovalStatus = "granted" if approve else "denied"
        settled = await self._close(
            approval, status, payload, by=by.principal, approved=approve, comment=comment
        )
        if settled is None:
            raise ApprovalClosedError(f"approval {approval_id} was closed a moment ago")
        return settled


def _approval_id(proposal: Proposal, site: CallSite) -> str:
    """The same for one call however often its task runs, and for nothing else."""
    action = request_hash({"action": proposal.action, "payload": proposal.payload})
    return ulid_for(f"{site.task}/{site.key('tool')}/{action}", ulid_time(site.task))


def _answer(event: Event, approval: str, proposal: Proposal) -> str:
    data: dict[str, object] = event.data if isinstance(event.data, dict) else {}  # type: ignore[assignment]
    if event.type == "approval.granted" or (event.type == "approval.expired" and data["approved"]):
        return approval
    if event.type == "approval.expired":
        raise ApprovalExpiredError(f"{proposal.action}: nobody approved it in time")
    raise ApprovalDeniedError(f"{proposal.action}: {event.type.split('.')[1]} by {event.principal}")

"""Where open approvals are kept, so that an inbox doesn't have to read the whole log (ADR-016).

The log says what happened to a run. This says what a person can still decide, and it is where
that gets decided: :meth:`ApprovalStore.settle` closes an approval and publishes the event in one
step, so of two people answering at once, one wins and only one answer reaches the run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime

from aios.core.envelope import Event
from aios.core.human import Approval, ApprovalStatus
from aios.fabric.port import EventBus

INBOX_LIMIT = 500
OVERDUE_BATCH = 100


class ApprovalStore(ABC):
    @abstractmethod
    async def add(self, approval: Approval) -> None:
        """Keep a new request. Keeping one again changes nothing, whatever became of it."""

    @abstractmethod
    async def get(self, tenant: str, approval_id: str) -> Approval | None: ...

    @abstractmethod
    async def find(
        self, tenant: str, status: ApprovalStatus, *, limit: int = INBOX_LIMIT
    ) -> list[Approval]:
        """A tenant's approvals in ``status``, oldest first."""

    @abstractmethod
    async def in_run(self, tenant: str, run_id: str) -> list[Approval]:
        """A run's pending approvals."""

    @abstractmethod
    async def overdue(self, now: datetime, *, limit: int = OVERDUE_BATCH) -> list[Approval]:
        """Pending approvals past their time, in every tenant."""

    @abstractmethod
    async def settle(
        self,
        approval: Approval,
        *,
        status: ApprovalStatus,
        approved: bool,
        by: str,
        comment: str | None,
        at: datetime,
        event: Event,
    ) -> Approval | None:
        """Close a pending approval and publish ``event``, as one atomic step.

        ``None`` means someone else closed it first, and nothing was published.
        """


class InMemoryApprovalStore(ApprovalStore):
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._approvals: dict[tuple[str, str], Approval] = {}

    async def add(self, approval: Approval) -> None:
        self._approvals.setdefault((approval.tenant, approval.id), approval)

    async def get(self, tenant: str, approval_id: str) -> Approval | None:
        return self._approvals.get((tenant, approval_id))

    async def find(
        self, tenant: str, status: ApprovalStatus, *, limit: int = INBOX_LIMIT
    ) -> list[Approval]:
        found = [a for a in self._approvals.values() if a.tenant == tenant and a.status == status]
        return sorted(found, key=lambda approval: approval.id)[:limit]

    async def in_run(self, tenant: str, run_id: str) -> list[Approval]:
        return [a for a in await self.find(tenant, "pending") if a.run == run_id]

    async def overdue(self, now: datetime, *, limit: int = OVERDUE_BATCH) -> list[Approval]:
        pending = (a for a in self._approvals.values() if a.status == "pending")
        return sorted(
            (a for a in pending if a.expires_at <= now), key=lambda approval: approval.expires_at
        )[:limit]

    async def settle(
        self,
        approval: Approval,
        *,
        status: ApprovalStatus,
        approved: bool,
        by: str,
        comment: str | None,
        at: datetime,
        event: Event,
    ) -> Approval | None:
        key = (approval.tenant, approval.id)
        current = self._approvals.get(key)
        if current is None or current.status != "pending":
            return None
        settled = replace(
            current, status=status, approved=approved, by=by, comment=comment, decided_at=at
        )
        await self.bus.publish(event)  # first: if this fails, the approval is still open
        self._approvals[key] = settled
        return settled

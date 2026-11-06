"""The audit log (FR-SEC-08): an append-only, hash-chained record for each tenant.

Each entry is a governed event, exactly as it was published, and its hash covers the hash before
it, so changing, removing or reordering any entry breaks every hash after it. A head record per
tenant catches a chain whose tail was cut off, which a chain alone can't show.

:class:`AuditRecorder` copies events from the fabric in a durable consumer group, so a crash loses
nothing and a redelivered event is recorded once. An entry therefore appears shortly after its
event, not in the same transaction.

Someone who can rewrite the whole table can rebuild a consistent chain. The chain proves integrity
against everything short of that; exporting heads out of reach is what closes the gap (FR-SEC-08,
P1, SIEM export).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from aios.core.envelope import Event
from aios.core.errors import BusClosedError
from aios.fabric.port import Delivery, EventBus, Scope, Subscription

logger = logging.getLogger("aios.security")

GENESIS = "0" * 64
AUDITED = (
    ("policy.decision.>", "audit.policy"),
    ("tool.call.>", "audit.tools"),
    ("approval.>", "audit.approvals"),
    ("security.>", "audit.security"),
    ("app.>", "audit.apps"),
    ("memory.item.>", "audit.memory"),
    ("memory.recalled", "audit.memory-reads"),
    ("memory.document.>", "audit.memory-documents"),
    ("run.*.requested", "audit.runs"),
)
"""Policy decisions, tool calls, approvals, admin changes and data access (FR-SEC-08)."""


@dataclass(frozen=True, slots=True)
class AuditEntry:
    tenant: str
    seq: int
    """1-based, without gaps, per tenant."""
    event_id: str
    entry: str
    """The event as canonical JSON: the exact text the hash covers."""
    previous: str
    hash: str

    @property
    def event(self) -> Event:
        return Event.model_validate_json(self.entry)


@dataclass(frozen=True, slots=True)
class AuditReport:
    ok: bool
    entries: int
    head: str
    problem: str | None = None
    first_bad_seq: int | None = None


def canonical(event: Event) -> str:
    return json.dumps(
        event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def chained(previous: str, entry: str) -> str:
    return hashlib.sha256(f"{previous}\n{entry}".encode()).hexdigest()


def check_chain(
    entries: Sequence[AuditEntry], *, head_seq: int | None, head_hash: str | None
) -> AuditReport:
    """Recompute a tenant's chain from the start and compare it with its head record."""
    previous = GENESIS
    for index, entry in enumerate(entries, start=1):
        problem: str | None = None
        if entry.seq != index:
            problem = f"entry {index} is missing"
        elif entry.previous != previous:
            problem = f"entry {index} doesn't follow the one before it"
        elif chained(previous, entry.entry) != entry.hash:
            problem = f"entry {index} was changed"
        if problem is not None:
            return AuditReport(False, len(entries), previous, problem, index)
        previous = entry.hash
    if (head_seq or 0) != len(entries) or (head_hash or GENESIS) != previous:
        return AuditReport(
            False, len(entries), previous, f"the head expects {head_seq or 0} entries", None
        )
    return AuditReport(True, len(entries), previous)


class AuditLog(ABC):
    @abstractmethod
    async def append(self, event: Event) -> bool:
        """Record an event once; ``False`` if it is already in the chain."""

    @abstractmethod
    async def entries(
        self, tenant: str, *, after: int = 0, limit: int = 500
    ) -> list[AuditEntry]: ...

    @abstractmethod
    async def verify(self, tenant: str) -> AuditReport: ...


class InMemoryAuditLog(AuditLog):
    def __init__(self) -> None:
        self._chains: dict[str, list[AuditEntry]] = {}
        self._heads: dict[str, tuple[int, str]] = {}
        self._seen: set[tuple[str, str]] = set()

    async def append(self, event: Event) -> bool:
        tenant = event.tenantid
        if (tenant, event.id) in self._seen:
            return False
        seq, previous = self._heads.get(tenant, (0, GENESIS))
        text = canonical(event)
        entry = AuditEntry(tenant, seq + 1, event.id, text, previous, chained(previous, text))
        self._chains.setdefault(tenant, []).append(entry)
        self._heads[tenant] = (entry.seq, entry.hash)
        self._seen.add((tenant, event.id))
        return True

    async def entries(self, tenant: str, *, after: int = 0, limit: int = 500) -> list[AuditEntry]:
        return self._chains.get(tenant, [])[after : after + limit]

    async def verify(self, tenant: str) -> AuditReport:
        seq, head = self._heads.get(tenant, (0, GENESIS))
        return check_chain(self._chains.get(tenant, []), head_seq=seq, head_hash=head)

    def tamper_for_test(self, tenant: str, *, index: int) -> None:
        """Rewrite an entry as an attacker with write access would."""
        chain = self._chains[tenant]
        entry = chain[index]
        chain[index] = AuditEntry(
            entry.tenant,
            entry.seq,
            entry.event_id,
            entry.entry.replace('"tenantid"', '"tenantid" '),
            entry.previous,
            entry.hash,
        )


class AuditRecorder:
    """Copies governed events to the audit log. Run one wherever a worker runs."""

    def __init__(self, bus: EventBus, log: AuditLog, *, lease: float = 30.0) -> None:
        self.bus = bus
        self.log = log
        self.lease = lease
        self._subscriptions: list[Subscription] = []
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        for pattern, group in AUDITED:
            subscription = await self.bus.subscribe(
                Scope.platform(), pattern, group=group, ack_wait=self.lease
            )
            self._subscriptions.append(subscription)
            self._tasks.append(asyncio.create_task(self._consume(subscription)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for subscription in self._subscriptions:
            await subscription.close()
        self._subscriptions.clear()

    async def _consume(self, subscription: Subscription) -> None:
        with contextlib.suppress(BusClosedError):
            async for delivery in subscription:
                await self._record(delivery)

    async def _record(self, delivery: Delivery) -> None:
        try:
            await self.log.append(delivery.event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("could not record %s; will retry", delivery.event.id)
            await delivery.nack(reason="audit log unavailable")
            return
        await delivery.ack()

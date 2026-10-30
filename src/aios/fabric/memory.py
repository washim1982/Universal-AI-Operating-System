"""In-memory ``EventBus`` adapter: one process, nothing survives a restart.

Used by unit tests, the conformance suite and ``aios dev``. It implements the full port
contract (consumer groups, leases, per-run ordering, retries, dead letters, replay), so
behaviour proven against it carries over to the durable adapters.

All state changes happen synchronously between awaits on one event loop, so no locks are
needed. Waiting subscribers sleep on a per-group signal that is replaced on every change.
"""

from __future__ import annotations

import asyncio
import itertools
from bisect import insort
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from aios.core.catalog import DeadLetterAdded
from aios.core.envelope import Event
from aios.core.errors import BusClosedError, NotFoundError, SubscriptionConflictError
from aios.core.ids import new_ulid
from aios.core.topics import matches, validate_name, validate_pattern
from aios.fabric.port import (
    DEFAULT_MAX_HOPS,
    DeadLetter,
    Delivery,
    EventBus,
    RetryPolicy,
    Scope,
    StartFrom,
    StoredEvent,
    Subscription,
)
from aios.fabric.subjects import physical_pattern, physical_subject

DLQ_EVENT_TYPE = "system.dlq.added"

_GroupKey = tuple[str, str, str]  # (tenant, app, group name)
_tokens = itertools.count(1)


def _now() -> float:
    return asyncio.get_running_loop().time()


@dataclass(slots=True)
class _Record:
    stored: StoredEvent
    subject: str


@dataclass(slots=True)
class _Entry:
    record: _Record
    attempt: int = 0
    not_before: float = 0.0

    @property
    def position(self) -> int:
        return self.record.stored.position

    @property
    def key(self) -> str | None:
        return self.record.stored.event.runid


def _by_position(entry: _Entry) -> int:
    return entry.position


@dataclass(slots=True)
class _Lease:
    entry: _Entry
    member: object
    deadline: float


@dataclass(slots=True)
class _Dead:
    letter: DeadLetter
    group: _GroupKey
    record: _Record


class _Group:
    def __init__(
        self,
        key: _GroupKey,
        scope: Scope,
        pattern: str,
        *,
        durable: bool,
        ack_wait: float,
        retry: RetryPolicy,
        on_dead_letter: Callable[[_Group, _Entry, str], None],
    ) -> None:
        self.key = key
        self.scope = scope
        self.pattern = pattern
        self.physical = physical_pattern(scope.tenant, scope.app, pattern)
        self.durable = durable
        self.ack_wait = ack_wait
        self.retry = retry
        self.on_dead_letter = on_dead_letter
        self.pending: list[_Entry] = []
        self.leases: dict[int, _Lease] = {}
        self.members: set[object] = set()
        self.signal = asyncio.Event()

    @property
    def name(self) -> str:
        return self.key[2]

    def accepts(self, record: _Record) -> bool:
        return matches(self.physical, record.subject)

    def notify(self) -> None:
        self.signal.set()
        self.signal = asyncio.Event()

    def enqueue(self, entry: _Entry) -> None:
        insort(self.pending, entry, key=_by_position)
        self.notify()

    def claim(self, member: object, now: float) -> tuple[int, _Lease] | None:
        """Lease the first deliverable entry, keeping per-run order."""
        self.expire(now)
        busy = {lease.entry.key for lease in self.leases.values()}
        blocked: set[str] = set()
        for index, entry in enumerate(self.pending):
            key = entry.key
            if key is not None and (key in busy or key in blocked):
                continue
            if entry.not_before > now:
                if key is not None:
                    blocked.add(key)  # later events of this run wait behind it
                continue
            del self.pending[index]
            entry.attempt += 1
            token = next(_tokens)
            lease = _Lease(entry, member, now + self.ack_wait)
            self.leases[token] = lease
            return token, lease
        return None

    def next_wakeup(self, now: float) -> float | None:
        times = [entry.not_before for entry in self.pending if entry.not_before > now]
        times.extend(lease.deadline for lease in self.leases.values())
        return min(times, default=None)

    def expire(self, now: float) -> None:
        for token in [t for t, lease in self.leases.items() if lease.deadline <= now]:
            self.fail(token, now, delay=0.0, reason="ack deadline exceeded")

    def ack(self, token: int) -> None:
        if self.leases.pop(token, None) is not None:
            self.notify()

    def extend(self, token: int, now: float) -> None:
        lease = self.leases.get(token)
        if lease is not None:
            lease.deadline = now + self.ack_wait

    def fail(self, token: int, now: float, *, delay: float | None, reason: str) -> None:
        lease = self.leases.pop(token, None)
        if lease is None:
            return  # stale: the lease already expired and the event was redelivered
        entry = lease.entry
        if entry.attempt >= self.retry.max_deliveries:
            self.on_dead_letter(self, entry, reason)
        else:
            entry.not_before = now + (self.retry.backoff(entry.attempt) if delay is None else delay)
            insort(self.pending, entry, key=_by_position)
        self.notify()

    def release(self, member: object) -> None:
        """A member left: its unsettled deliveries go back to the queue immediately."""
        for token in [t for t, lease in self.leases.items() if lease.member is member]:
            entry = self.leases.pop(token).entry
            entry.not_before = 0.0
            insort(self.pending, entry, key=_by_position)
        self.notify()


class _MemoryDelivery(Delivery):
    def __init__(self, group: _Group, token: int, lease: _Lease) -> None:
        super().__init__(lease.entry.record.stored, attempt=lease.entry.attempt, group=group.name)
        self._group = group
        self._token = token

    async def ack(self) -> None:
        self._group.ack(self._token)

    async def nack(self, *, delay: float | None = None, reason: str = "nacked") -> None:
        self._group.fail(self._token, _now(), delay=delay, reason=reason)

    async def extend(self) -> None:
        self._group.extend(self._token, _now())


class _MemorySubscription(Subscription):
    def __init__(self, bus: InMemoryEventBus, group: _Group, on_close: Callable[[_Group], None]):
        super().__init__(group.scope, group.pattern, group.name)
        self._bus = bus
        self._group = group
        self._on_close = on_close
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self._bus.closed

    async def next(self, timeout: float | None = None) -> Delivery | None:
        deadline = None if timeout is None else _now() + timeout
        while not self.closed:
            now = _now()
            claimed = self._group.claim(self, now)
            if claimed is not None:
                return _MemoryDelivery(self._group, *claimed)
            wake = self._group.next_wakeup(now)
            if deadline is not None:
                if now >= deadline:
                    return None
                wake = deadline if wake is None else min(wake, deadline)
            signal = self._group.signal
            try:
                async with asyncio.timeout_at(wake):
                    await signal.wait()
            except TimeoutError:
                pass
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._group.members.discard(self)
        self._group.release(self)
        self._on_close(self._group)


class InMemoryEventBus(EventBus):
    """Validates catalog payloads on publish by default (dev mode, FR-EVT-10)."""

    def __init__(self, *, max_hops: int = DEFAULT_MAX_HOPS, validate_payloads: bool = True):
        super().__init__(max_hops=max_hops, validate_payloads=validate_payloads)
        self._log: list[_Record] = []
        self._groups: dict[_GroupKey, _Group] = {}
        self._dead: dict[str, _Dead] = {}
        self._closed = False

    # ------------------------------------------------------------------ publishing

    async def _append(self, batch: Sequence[tuple[Event, str]]) -> None:
        self._store(batch)

    def _store(self, batch: Sequence[tuple[Event, str]]) -> None:
        for event, subject in batch:
            record = _Record(StoredEvent(len(self._log) + 1, event), subject)
            self._log.append(record)
            for group in self._groups.values():
                if group.accepts(record):
                    group.enqueue(_Entry(record))

    # ------------------------------------------------------------------ consuming

    async def subscribe(
        self,
        scope: Scope,
        pattern: str,
        *,
        group: str | None = None,
        start: StartFrom = "new",
        ack_wait: float = 30.0,
        retry: RetryPolicy | None = None,
    ) -> Subscription:
        if self._closed:
            raise BusClosedError("the event bus is closed")
        validate_pattern(pattern)
        if group is not None:
            validate_name(group, "group")
        key: _GroupKey = (scope.tenant, scope.app, group or f"_inbox.{new_ulid()}")
        target = self._groups.get(key)
        if target is None:
            target = _Group(
                key,
                scope,
                pattern,
                durable=group is not None,
                ack_wait=ack_wait,
                retry=retry or RetryPolicy(),
                on_dead_letter=self._dead_letter,
            )
            target.pending.extend(_Entry(r) for r in self._backlog(start) if target.accepts(r))
            self._groups[key] = target
        elif target.pattern != pattern:
            raise SubscriptionConflictError(
                f"group {group!r} subscribes to {target.pattern!r}, not {pattern!r}"
            )
        member = _MemorySubscription(self, target, self._member_left)
        target.members.add(member)
        return member

    def _backlog(self, start: StartFrom) -> list[_Record]:
        if start == "new":
            return []
        if start == "earliest":
            return list(self._log)
        if isinstance(start, int):
            return self._log[max(start, 0) :]
        return [record for record in self._log if record.stored.event.time >= start]

    def _member_left(self, group: _Group) -> None:
        if not group.durable and not group.members:
            self._groups.pop(group.key, None)

    async def replay(
        self,
        scope: Scope,
        pattern: str = ">",
        *,
        after: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        run_id: str | None = None,
    ) -> AsyncIterator[StoredEvent]:
        physical = physical_pattern(scope.tenant, scope.app, pattern)
        for record in self._log[max(after or 0, 0) :]:
            event = record.stored.event
            if since is not None and event.time < since:
                continue
            if until is not None and event.time >= until:
                continue
            if run_id is not None and event.runid != run_id:
                continue
            if matches(physical, record.subject):
                yield record.stored

    # ------------------------------------------------------------------ dead letters

    def _dead_letter(self, group: _Group, entry: _Entry, reason: str) -> None:
        stored = entry.record.stored
        letter = DeadLetter(
            id=new_ulid(),
            group=group.name,
            stored=stored,
            attempts=entry.attempt,
            reason=reason,
            dead_at=datetime.now(UTC),
        )
        self._dead[letter.id] = _Dead(letter, group.key, entry.record)
        event = stored.event
        if event.type == DLQ_EVENT_TYPE:
            return  # never cascade dead-letter notices
        payload = DeadLetterAdded(
            dead_letter=letter.id,
            group=letter.group,
            event=event.id,
            event_type=event.type,
            attempts=letter.attempts,
            reason=reason,
            dead_at=letter.dead_at,
        )
        notice = event.derive(
            DLQ_EVENT_TYPE,
            payload.model_dump(mode="json"),
            source=f"aios://{event.tenantid}/system/fabric",
            principal="system:fabric",
        )
        # Internal fact: stored directly, so a notice about an event at the hop limit still lands.
        self._store([(notice, physical_subject(notice))])

    def _take(self, scope: Scope, dead_letter_id: str) -> _Dead:
        dead = self._dead.get(dead_letter_id)
        if dead is None or not scope.contains(dead.letter.stored.event):
            raise NotFoundError(f"no dead letter {dead_letter_id!r} in scope")
        return self._dead.pop(dead_letter_id)

    async def dead_letters(self, scope: Scope) -> list[DeadLetter]:
        return [d.letter for d in self._dead.values() if scope.contains(d.letter.stored.event)]

    async def redrive(self, scope: Scope, dead_letter_id: str) -> None:
        dead = self._take(scope, dead_letter_id)
        group = self._groups.get(dead.group)
        if group is None:
            self._dead[dead_letter_id] = dead
            raise NotFoundError(f"consumer group {dead.letter.group!r} no longer exists")
        group.enqueue(_Entry(dead.record))

    async def discard(self, scope: Scope, dead_letter_id: str) -> None:
        self._take(scope, dead_letter_id)

    # ------------------------------------------------------------------ lifecycle

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True
        for group in self._groups.values():
            group.notify()

"""The ``EventBus`` port (FR-EVT-15, ADR-005): one interface and one conformance suite for every
backend (in-memory now; PostgreSQL in WP 1.1; NATS JetStream in WP 4.1).

Delivery model:

* **At least once.** A delivery is leased to one subscriber; if it isn't acked within
  ``ack_wait`` it is redelivered. Consumers deduplicate by event id (the SDK inbox, FR-EVT-06).
* **Consumer groups.** Subscribers that pass the same ``group`` (within the same scope) compete:
  each event goes to one of them. A group is durable: it keeps receiving events while no member
  is connected. ``group=None`` creates a private, ephemeral group, which is how broadcast works.
* **Ordering per run.** Within a group, events with the same ``runid`` are delivered one at a
  time, in publish order (FR-EVT-05). Events without a run id are not ordered.
* **Retries and dead letters.** ``nack`` redelivers after an exponential backoff with jitter.
  After ``max_deliveries`` the event is dead-lettered and a ``system.dlq.added`` fact is
  published (FR-EVT-08).
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Literal, Self

from pydantic import ValidationError

from aios.core import catalog
from aios.core.envelope import Event
from aios.core.errors import (
    BusClosedError,
    HopLimitExceededError,
    InvalidEventError,
    RequestTimeoutError,
)
from aios.core.topics import COMMAND_VERB, EventKind, is_dns_label
from aios.fabric.subjects import physical_subject

ANY = "*"
DEFAULT_MAX_HOPS = 64

StartFrom = Literal["new", "earliest"] | int | datetime
"""Where a new consumer group starts: new events only, the whole log, after a position, or at a
time. Ignored when joining an existing group."""


@dataclass(frozen=True, slots=True)
class Scope:
    """The tenant and app a subscriber may see (FR-EVT-04).

    ``"*"`` widens a field to every tenant or app; only platform components use that.
    """

    tenant: str
    app: str = ANY

    def __post_init__(self) -> None:
        for value, what in ((self.tenant, "tenant"), (self.app, "app")):
            if value != ANY and not is_dns_label(value):
                raise ValueError(f"invalid {what} id: {value!r}")

    @classmethod
    def platform(cls) -> Scope:
        """Every tenant and every app."""
        return cls(ANY, ANY)

    def contains(self, event: Event) -> bool:
        return self.tenant in (ANY, event.tenantid) and self.app in (ANY, event.appid)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_deliveries: int = 5
    initial_backoff: float = 0.5
    max_backoff: float = 60.0
    multiplier: float = 2.0
    jitter: bool = True

    def backoff(self, attempt: int) -> float:
        """Seconds to wait before redelivering after failed delivery ``attempt`` (1-based).

        Capped exponential growth with "equal jitter": a random value between half the
        capped delay and the full capped delay.
        """
        try:
            uncapped = self.initial_backoff * self.multiplier ** (attempt - 1)
        except OverflowError:  # very large attempt numbers, e.g. with "retry forever" policies
            uncapped = self.max_backoff
        capped = min(self.max_backoff, uncapped)
        if not self.jitter:
            return capped
        return capped / 2 + random.uniform(0, capped / 2)  # noqa: S311 - jitter, not crypto


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """An event as stored in the log. ``position`` increases in commit order."""

    position: int
    event: Event


@dataclass(frozen=True, slots=True)
class DeadLetter:
    id: str
    group: str
    stored: StoredEvent
    attempts: int
    reason: str
    dead_at: datetime


class Delivery(ABC):
    """One delivery of an event to one subscriber. Settle it before ``ack_wait`` runs out."""

    def __init__(self, stored: StoredEvent, *, attempt: int, group: str) -> None:
        self.stored = stored
        self.attempt = attempt
        self.group = group

    @property
    def event(self) -> Event:
        return self.stored.event

    @property
    def position(self) -> int:
        return self.stored.position

    @abstractmethod
    async def ack(self) -> None:
        """Processing succeeded. An ack after the lease expired is ignored (it was redelivered)."""

    @abstractmethod
    async def nack(self, *, delay: float | None = None, reason: str = "nacked") -> None:
        """Processing failed: redeliver after ``delay`` (default: the retry policy's backoff),
        or dead-letter the event once it has been delivered ``max_deliveries`` times."""

    @abstractmethod
    async def extend(self) -> None:
        """Still working: restart the ack deadline."""


class Subscription(ABC):
    """A member of a consumer group. Iterate it, or call :meth:`next` with a timeout."""

    def __init__(self, scope: Scope, pattern: str, group: str) -> None:
        self.scope = scope
        self.pattern = pattern
        self.group = group

    @property
    @abstractmethod
    def closed(self) -> bool: ...

    @abstractmethod
    async def next(self, timeout: float | None = None) -> Delivery | None:
        """The next delivery, or ``None`` if ``timeout`` elapses or the subscription closes."""

    @abstractmethod
    async def close(self) -> None:
        """Leave the group. Unsettled deliveries are redelivered to the remaining members."""

    async def _iterate(self) -> AsyncIterator[Delivery]:
        while (delivery := await self.next()) is not None:
            yield delivery

    def __aiter__(self) -> AsyncIterator[Delivery]:
        return self._iterate()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


class EventBus(ABC):
    """The fabric port. Adapters implement the abstract methods and pass the conformance suite."""

    def __init__(
        self, *, max_hops: int = DEFAULT_MAX_HOPS, validate_payloads: bool = False
    ) -> None:
        self.max_hops = max_hops
        self.validate_payloads = validate_payloads

    # ------------------------------------------------------------------ publishing

    async def publish(self, *events: Event) -> None:
        """Publish events in order, atomically: either every event is stored or none is.

        Raises :class:`HopLimitExceededError` for events past the hop limit (FR-EVT-11) and
        :class:`InvalidEventError` for catalog payload mismatches when validation is on.
        """
        if self.closed:
            raise BusClosedError("the event bus is closed")
        batch = [(self._check(event), physical_subject(event)) for event in events]
        await self._append(batch)

    def _check(self, event: Event) -> Event:
        if event.hopcount > self.max_hops:
            raise HopLimitExceededError(event.hopcount, self.max_hops)
        if self.validate_payloads:
            spec = catalog.get(event.type)
            if spec is not None and spec.payload is not None:
                try:
                    spec.payload.model_validate(event.data)
                except ValidationError as exc:
                    raise InvalidEventError(f"invalid {event.type} payload: {exc}") from exc
        return event

    @abstractmethod
    async def _append(self, batch: Sequence[tuple[Event, str]]) -> None:
        """Store validated ``(event, physical subject)`` pairs atomically, in order."""

    # ------------------------------------------------------------------ consuming

    @abstractmethod
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
        """Join (or create) a consumer group receiving events that match ``pattern`` in ``scope``.

        Joining an existing group with a different pattern raises
        :class:`SubscriptionConflictError`. ``start``, ``ack_wait`` and ``retry`` apply when
        the group is created.
        """

    @abstractmethod
    def replay(
        self,
        scope: Scope,
        pattern: str = ">",
        *,
        after: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        run_id: str | None = None,
    ) -> AsyncIterator[StoredEvent]:
        """Read stored events in order, by position, time range or run (FR-EVT-09)."""

    # ------------------------------------------------------------------ dead letters

    @abstractmethod
    async def dead_letters(self, scope: Scope) -> list[DeadLetter]: ...

    @abstractmethod
    async def redrive(self, scope: Scope, dead_letter_id: str) -> None:
        """Put a dead-lettered event back on its group's queue with a fresh delivery count."""

    @abstractmethod
    async def discard(self, scope: Scope, dead_letter_id: str) -> None: ...

    # ------------------------------------------------------------------ request / reply

    async def request(
        self,
        event: Event,
        *,
        timeout: float = 30.0,
        reply_types: Iterable[str] | None = None,
    ) -> Event:
        """Publish a command and wait for the first reply caused by it (FR-EVT-12).

        A reply is an event whose ``causationid`` is the command's id. By default the reply
        types for ``x.requested`` are ``x.completed`` and ``x.failed``.
        """
        if event.kind is not EventKind.COMMAND:
            raise InvalidEventError(f"request() needs a command (*.{COMMAND_VERB}): {event.type}")
        if reply_types is None:
            base = event.type.removesuffix(f".{COMMAND_VERB}")
            reply_types = (f"{base}.completed", f"{base}.failed")
        replies = tuple(reply_types)
        if not replies:
            raise ValueError("request() needs at least one reply type")
        scope = Scope(event.tenantid, event.appid)
        async with AsyncExitStack() as stack:
            subscriptions = [
                await stack.enter_async_context(await self.subscribe(scope, reply_type))
                for reply_type in replies
            ]
            await self.publish(event)
            try:
                async with asyncio.timeout(timeout):
                    return await _first_reply(subscriptions, event.id)
            except TimeoutError:
                raise RequestTimeoutError(f"no reply to {event.type} {event.id}") from None

    # ------------------------------------------------------------------ lifecycle

    @property
    @abstractmethod
    def closed(self) -> bool: ...

    @abstractmethod
    async def close(self) -> None:
        """Stop the bus. Open subscriptions end; further publishes raise BusClosedError."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


async def _first_reply(subscriptions: Sequence[Subscription], request_id: str) -> Event:
    async def watch(subscription: Subscription) -> Event:
        async for delivery in subscription:
            await delivery.ack()
            if delivery.event.causationid == request_id:
                return delivery.event
        raise BusClosedError("the event bus closed while waiting for a reply")

    tasks = [asyncio.create_task(watch(subscription)) for subscription in subscriptions]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return done.pop().result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

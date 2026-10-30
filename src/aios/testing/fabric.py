"""EventBus conformance suite (FR-EVT-15, NFR-11).

Every ``EventBus`` adapter must pass this suite unchanged. To run it against an adapter,
subclass :class:`EventBusConformance` in a test module and provide a ``bus`` fixture::

    class TestMyBus(EventBusConformance):
        @pytest.fixture
        async def bus(self) -> AsyncIterator[EventBus]:
            async with MyBus(...) as bus:
                yield bus

Timing-based cases use short leases and backoffs; adapters with coarse polling may override
``ack_wait`` and ``patience`` on the subclass.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from aios.core.envelope import Event
from aios.core.errors import (
    BusClosedError,
    HopLimitExceededError,
    InvalidEventError,
    NotFoundError,
    RequestTimeoutError,
    SubscriptionConflictError,
)
from aios.core.ids import new_ulid
from aios.fabric.port import Delivery, EventBus, RetryPolicy, Scope, StoredEvent, Subscription
from aios.testing import DEFAULT_APP, DEFAULT_TENANT, make_event, task_request

SCOPE = Scope(DEFAULT_TENANT, DEFAULT_APP)


async def _collect(stream: Subscription, count: int, patience: float) -> list[Delivery]:
    deliveries: list[Delivery] = []
    for _ in range(count):
        delivery = await stream.next(timeout=patience)
        assert delivery is not None, f"expected {count} deliveries, got {len(deliveries)}"
        await delivery.ack()
        deliveries.append(delivery)
    return deliveries


async def _replayed(
    bus: EventBus,
    scope: Scope = SCOPE,
    *,
    after: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    run_id: str | None = None,
) -> list[StoredEvent]:
    replay = bus.replay(scope, after=after, since=since, until=until, run_id=run_id)
    return [stored async for stored in replay]


class EventBusConformance:
    """The shared contract. Subclasses provide the ``bus`` fixture."""

    ack_wait: float = 0.2
    """Lease used by the redelivery cases."""
    patience: float = 2.0
    """How long to wait for a delivery that must arrive."""
    quiet: float = 0.1
    """How long to wait to be confident that nothing arrives."""

    @property
    def fast_retry(self) -> RetryPolicy:
        return RetryPolicy(max_deliveries=3, initial_backoff=0.01, max_backoff=0.02, jitter=False)

    async def _next(self, stream: Subscription) -> Delivery:
        delivery = await stream.next(timeout=self.patience)
        assert delivery is not None, "expected a delivery"
        return delivery

    # ------------------------------------------------------------------ publish / subscribe

    async def test_delivers_matching_events_only(self, bus: EventBus) -> None:
        stream = await bus.subscribe(SCOPE, "research.>")
        await bus.publish(make_event("research.plan.created"), make_event("other.thing.happened"))
        delivery = await self._next(stream)
        assert delivery.event.type == "research.plan.created"
        assert delivery.attempt == 1
        await delivery.ack()
        assert await stream.next(timeout=self.quiet) is None

    async def test_star_matches_one_token_and_rest_matches_the_remainder(
        self, bus: EventBus
    ) -> None:
        one = await bus.subscribe(SCOPE, "research.*.created")
        rest = await bus.subscribe(SCOPE, "research.>")
        await bus.publish(
            make_event("research.plan.created"), make_event("research.plan.draft.created")
        )
        assert [d.event.type for d in await _collect(one, 1, self.patience)] == [
            "research.plan.created"
        ]
        assert await one.next(timeout=self.quiet) is None
        assert len(await _collect(rest, 2, self.patience)) == 2

    async def test_task_requests_are_routed_by_capability(self, bus: EventBus) -> None:
        research = await bus.subscribe(SCOPE, "task.requested.research.>", group="research")
        await bus.publish(task_request("research.web"), task_request("writing"))
        delivery = await self._next(research)
        assert isinstance(delivery.event.data, dict)
        assert delivery.event.data["capability"] == "research.web"
        await delivery.ack()
        assert await research.next(timeout=self.quiet) is None

    async def test_preserves_the_envelope(self, bus: EventBus) -> None:
        stream = await bus.subscribe(SCOPE, ">")
        sent = make_event(
            "research.plan.created",
            {"tasks": ["a", "b"], "nested": {"n": 1}},
            run_id=new_ulid(),
            onbehalfof="user:alice@acme",
            idempotencykey="plan-1",
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        )
        await bus.publish(sent)
        received = (await self._next(stream)).event
        assert received == sent

    async def test_new_groups_start_after_existing_events_by_default(self, bus: EventBus) -> None:
        await bus.publish(make_event())
        stream = await bus.subscribe(SCOPE, ">")
        assert await stream.next(timeout=self.quiet) is None

    async def test_start_earliest_backfills_the_log(self, bus: EventBus) -> None:
        first, second = make_event(), make_event()
        await bus.publish(first, second)
        stream = await bus.subscribe(SCOPE, ">", group="late", start="earliest")
        ids = [d.event.id for d in await _collect(stream, 2, self.patience)]
        assert ids == [first.id, second.id]

    async def test_start_after_a_position(self, bus: EventBus) -> None:
        await bus.publish(make_event(), make_event())
        position = (await _replayed(bus))[0].position
        stream = await bus.subscribe(SCOPE, ">", group="partial", start=position)
        deliveries = await _collect(stream, 1, self.patience)
        assert deliveries[0].position > position
        assert await stream.next(timeout=self.quiet) is None

    # ------------------------------------------------------------------ isolation (FR-EVT-04)

    async def test_tenants_are_isolated(self, bus: EventBus) -> None:
        acme = await bus.subscribe(Scope("acme"), ">")
        globex = await bus.subscribe(Scope("globex"), ">")
        await bus.publish(make_event(tenant="acme"), make_event(tenant="globex"))
        assert (await self._next(acme)).event.tenantid == "acme"
        assert (await self._next(globex)).event.tenantid == "globex"
        assert await acme.next(timeout=self.quiet) is None
        assert await globex.next(timeout=self.quiet) is None

    async def test_app_scope_excludes_other_apps(self, bus: EventBus) -> None:
        stream = await bus.subscribe(Scope("acme", "ai-researcher"), ">")
        await bus.publish(make_event(app="ai-support-desk"), make_event(app="ai-researcher"))
        assert (await self._next(stream)).event.appid == "ai-researcher"
        assert await stream.next(timeout=self.quiet) is None

    async def test_platform_scope_sees_every_tenant(self, bus: EventBus) -> None:
        stream = await bus.subscribe(Scope.platform(), ">")
        await bus.publish(make_event(tenant="acme"), make_event(tenant="globex"))
        tenants = {d.event.tenantid for d in await _collect(stream, 2, self.patience)}
        assert tenants == {"acme", "globex"}

    async def test_same_group_name_in_two_tenants_is_two_groups(self, bus: EventBus) -> None:
        acme = await bus.subscribe(Scope("acme"), ">", group="workers")
        globex = await bus.subscribe(Scope("globex"), ">", group="workers")
        await bus.publish(make_event(tenant="acme"), make_event(tenant="globex"))
        assert (await self._next(acme)).event.tenantid == "acme"
        assert (await self._next(globex)).event.tenantid == "globex"

    # ------------------------------------------------------------------ consumer groups

    async def test_group_members_compete(self, bus: EventBus) -> None:
        first = await bus.subscribe(SCOPE, "work.>", group="workers")
        second = await bus.subscribe(SCOPE, "work.>", group="workers")
        sent = [make_event("work.item.created") for _ in range(6)]
        await bus.publish(*sent)
        held = await self._next(first)  # not acked: the other member must get a different event
        other = await self._next(second)
        assert held.event.id != other.event.id
        await held.ack()
        await other.ack()
        rest = [await self._next(first) for _ in range(4)]
        for delivery in rest:
            await delivery.ack()
        received = [held.event.id, other.event.id, *(d.event.id for d in rest)]
        assert sorted(received) == sorted(e.id for e in sent)
        assert await second.next(timeout=self.quiet) is None

    async def test_every_group_receives_every_fact(self, bus: EventBus) -> None:
        audit = await bus.subscribe(SCOPE, ">", group="audit")
        index = await bus.subscribe(SCOPE, ">", group="indexer")
        sent = make_event()
        await bus.publish(sent)
        assert (await self._next(audit)).event.id == sent.id
        assert (await self._next(index)).event.id == sent.id

    async def test_durable_group_keeps_events_while_no_member_is_connected(
        self, bus: EventBus
    ) -> None:
        stream = await bus.subscribe(SCOPE, ">", group="durable")
        await stream.close()
        sent = make_event()
        await bus.publish(sent)
        rejoined = await bus.subscribe(SCOPE, ">", group="durable")
        assert (await self._next(rejoined)).event.id == sent.id

    async def test_joining_a_group_with_another_pattern_conflicts(self, bus: EventBus) -> None:
        await bus.subscribe(SCOPE, "work.>", group="workers")
        with pytest.raises(SubscriptionConflictError):
            await bus.subscribe(SCOPE, "other.>", group="workers")

    async def test_closing_a_member_redelivers_its_unsettled_events(self, bus: EventBus) -> None:
        leaving = await bus.subscribe(SCOPE, ">", group="workers")
        staying = await bus.subscribe(SCOPE, ">", group="workers")
        sent = make_event()
        await bus.publish(sent)
        assert (await self._next(leaving)).event.id == sent.id
        await leaving.close()
        delivery = await self._next(staying)
        assert delivery.event.id == sent.id
        assert delivery.attempt == 2

    # ------------------------------------------------------------------ ordering (FR-EVT-05)

    async def test_one_run_is_delivered_in_order_and_one_at_a_time(self, bus: EventBus) -> None:
        first = await bus.subscribe(SCOPE, ">", group="workers")
        second = await bus.subscribe(SCOPE, ">", group="workers")
        run, other_run = new_ulid(), new_ulid()
        step1, step2 = make_event(run_id=run), make_event(run_id=run)
        unrelated = make_event(run_id=other_run)
        await bus.publish(step1, step2, unrelated)

        held = await self._next(first)
        assert held.event.id == step1.id
        parallel = await self._next(second)
        assert parallel.event.id == unrelated.id  # step2 waits behind step1
        await parallel.ack()
        assert await second.next(timeout=self.quiet) is None
        await held.ack()
        assert (await self._next(second)).event.id == step2.id

    async def test_retries_keep_run_order(self, bus: EventBus) -> None:
        stream = await bus.subscribe(SCOPE, ">", group="workers", retry=self.fast_retry)
        run = new_ulid()
        step1, step2 = make_event(run_id=run), make_event(run_id=run)
        await bus.publish(step1, step2)
        await (await self._next(stream)).nack(delay=self.quiet)
        retried = await self._next(stream)
        assert (retried.event.id, retried.attempt) == (step1.id, 2)
        await retried.ack()
        assert (await self._next(stream)).event.id == step2.id

    # ------------------------------------------------------------------ acks and retries

    async def test_nack_redelivers_with_the_next_attempt_number(self, bus: EventBus) -> None:
        stream = await bus.subscribe(SCOPE, ">", group="workers", retry=self.fast_retry)
        await bus.publish(make_event())
        await (await self._next(stream)).nack(delay=0)
        assert (await self._next(stream)).attempt == 2

    async def test_nack_without_delay_uses_the_backoff(self, bus: EventBus) -> None:
        backoff = RetryPolicy(initial_backoff=self.quiet * 3, jitter=False)
        stream = await bus.subscribe(SCOPE, ">", group="workers", retry=backoff)
        await bus.publish(make_event())
        await (await self._next(stream)).nack()
        assert await stream.next(timeout=self.quiet) is None
        assert (await self._next(stream)).attempt == 2

    async def test_unacked_deliveries_are_redelivered_after_ack_wait(self, bus: EventBus) -> None:
        stream = await bus.subscribe(SCOPE, ">", group="workers", ack_wait=self.ack_wait)
        sent = make_event()
        await bus.publish(sent)
        await self._next(stream)  # never settled
        redelivered = await self._next(stream)
        assert (redelivered.event.id, redelivered.attempt) == (sent.id, 2)

    async def test_extend_keeps_the_lease(self, bus: EventBus) -> None:
        holder = await bus.subscribe(SCOPE, ">", group="workers", ack_wait=self.ack_wait)
        other = await bus.subscribe(SCOPE, ">", group="workers")
        await bus.publish(make_event())
        delivery = await self._next(holder)
        await asyncio.sleep(self.ack_wait * 0.6)
        await delivery.extend()
        await asyncio.sleep(self.ack_wait * 0.6)
        assert await other.next(timeout=self.ack_wait * 0.1) is None
        await delivery.ack()

    async def test_an_ack_for_an_expired_lease_is_ignored(self, bus: EventBus) -> None:
        stream = await bus.subscribe(
            SCOPE, ">", group="workers", ack_wait=self.ack_wait, retry=self.fast_retry
        )
        await bus.publish(make_event())
        expired = await self._next(stream)
        current = await self._next(stream)  # redelivered once the lease ran out
        await expired.ack()
        await current.nack(delay=0)  # still in flight, so this nack counts
        assert (await self._next(stream)).attempt == 3

    async def test_dead_letters_after_max_deliveries(self, bus: EventBus) -> None:
        notices = await bus.subscribe(SCOPE, "system.dlq.added")
        stream = await bus.subscribe(SCOPE, "work.>", group="workers", retry=self.fast_retry)
        sent = make_event("work.item.created")
        await bus.publish(sent)
        for _ in range(self.fast_retry.max_deliveries):
            await (await self._next(stream)).nack(delay=0, reason="boom")
        assert await stream.next(timeout=self.quiet) is None

        [letter] = await bus.dead_letters(SCOPE)
        assert (letter.stored.event.id, letter.attempts, letter.group) == (sent.id, 3, "workers")
        assert letter.reason == "boom"
        notice = (await self._next(notices)).event
        assert notice.causationid == sent.id
        assert notice.hopcount == sent.hopcount + 1
        assert isinstance(notice.data, dict)
        assert notice.data["dead_letter"] == letter.id

    async def test_redrive_and_discard_dead_letters(self, bus: EventBus) -> None:
        retry = RetryPolicy(max_deliveries=1)
        stream = await bus.subscribe(SCOPE, "work.>", group="workers", retry=retry)
        keep, drop = make_event("work.item.created"), make_event("work.item.created")
        await bus.publish(keep, drop)
        for _ in range(2):
            await (await self._next(stream)).nack(delay=0)
        letters = {letter.stored.event.id: letter for letter in await bus.dead_letters(SCOPE)}
        assert set(letters) == {keep.id, drop.id}

        await bus.redrive(SCOPE, letters[keep.id].id)
        await bus.discard(SCOPE, letters[drop.id].id)
        redriven = await self._next(stream)
        assert (redriven.event.id, redriven.attempt) == (keep.id, 1)
        await redriven.ack()
        assert await bus.dead_letters(SCOPE) == []
        with pytest.raises(NotFoundError):
            await bus.discard(SCOPE, letters[drop.id].id)

    async def test_dead_letters_are_tenant_scoped(self, bus: EventBus) -> None:
        stream = await bus.subscribe(SCOPE, ">", group="workers", retry=RetryPolicy(1))
        await bus.publish(make_event())
        await (await self._next(stream)).nack(delay=0)
        [letter] = await bus.dead_letters(SCOPE)
        other = Scope("globex")
        assert await bus.dead_letters(other) == []
        with pytest.raises(NotFoundError):
            await bus.redrive(other, letter.id)
        with pytest.raises(NotFoundError):
            await bus.discard(other, letter.id)

    # ------------------------------------------------------------------ publish validation

    async def test_rejects_events_past_the_hop_limit(self, bus: EventBus) -> None:
        await bus.publish(make_event(hopcount=bus.max_hops))
        with pytest.raises(HopLimitExceededError) as raised:
            await bus.publish(make_event(hopcount=bus.max_hops + 1))
        assert raised.value.code == "budget.exceeded"

    async def test_batch_publish_is_atomic(self, bus: EventBus) -> None:
        with pytest.raises(HopLimitExceededError):
            await bus.publish(make_event(), make_event(hopcount=bus.max_hops + 1))
        assert await _replayed(bus) == []

    async def test_long_names_match_and_do_not_break_publishing(self, bus: EventBus) -> None:
        # Valid logical names can exceed the topic limits once the scope is prefixed.
        tenant, app = "t" * 63, "a" * 63
        prefix = ".".join(["x" * 15] * 15)
        long = await bus.subscribe(Scope(tenant, app), f"{prefix}.>")
        await bus.publish(make_event(), make_event(f"{prefix}.y", tenant=tenant, app=app))
        assert (await self._next(long)).event.type == f"{prefix}.y"

    async def test_rejects_a_task_request_without_a_capability(self, bus: EventBus) -> None:
        with pytest.raises(InvalidEventError):
            await bus.publish(make_event("task.requested", {"input": 1}))

    # ------------------------------------------------------------------ replay (FR-EVT-09)

    async def test_replay_by_position_time_and_run(self, bus: EventBus) -> None:
        run = new_ulid()
        early = make_event("research.plan.created", run_id=run)
        late = make_event(
            "research.plan.created", time=early.time + timedelta(minutes=5), run_id=new_ulid()
        )
        other = make_event("other.thing.happened", run_id=run)
        await bus.publish(early, late, other)

        everything = await _replayed(bus)
        assert [s.event.id for s in everything] == [early.id, late.id, other.id]
        assert [s.position for s in everything] == sorted(s.position for s in everything)
        after_first = await _replayed(bus, after=everything[0].position)
        assert [s.event.id for s in after_first] == [late.id, other.id]
        by_run = await _replayed(bus, run_id=run)
        assert [s.event.id for s in by_run] == [early.id, other.id]
        window = await _replayed(bus, since=late.time, until=late.time + timedelta(seconds=1))
        assert [s.event.id for s in window] == [late.id]
        by_pattern = [s async for s in bus.replay(SCOPE, "research.>")]
        assert [s.event.id for s in by_pattern] == [early.id, late.id]

    async def test_replay_is_scoped(self, bus: EventBus) -> None:
        await bus.publish(make_event(tenant="acme"), make_event(tenant="globex"))
        assert [s.event.tenantid for s in await _replayed(bus, Scope("globex"))] == ["globex"]

    # ------------------------------------------------------------------ request / reply

    async def test_request_returns_the_reply(self, bus: EventBus) -> None:
        workers = await bus.subscribe(SCOPE, "task.requested.>", group="workers")

        async def serve() -> None:
            delivery = await self._next(workers)
            await bus.publish(
                delivery.event.derive(
                    "task.completed",
                    {"capability": "research.web", "output": {"summary": "done"}},
                    source="aios://acme/apps/ai-researcher/agents/research-agent",
                    principal="agent:research-agent@acme",
                )
            )
            await delivery.ack()

        server = asyncio.create_task(serve())
        command = task_request("research.web", {"question": "why?"})
        reply = await bus.request(command, timeout=self.patience)
        await server
        assert reply.type == "task.completed"
        assert reply.causationid == command.id

    async def test_request_times_out(self, bus: EventBus) -> None:
        with pytest.raises(RequestTimeoutError):
            await bus.request(task_request("research.web"), timeout=self.quiet)

    async def test_request_needs_a_command(self, bus: EventBus) -> None:
        with pytest.raises(InvalidEventError):
            await bus.request(make_event("research.plan.created"), timeout=self.quiet)

    # ------------------------------------------------------------------ lifecycle

    async def test_iterating_a_subscription(self, bus: EventBus) -> None:
        stream = await bus.subscribe(SCOPE, ">")
        sent = [make_event() for _ in range(3)]
        await bus.publish(*sent)
        received: list[Event] = []
        async for delivery in stream:
            await delivery.ack()
            received.append(delivery.event)
            if len(received) == len(sent):
                break
        assert [e.id for e in received] == [e.id for e in sent]

    async def test_close_ends_subscriptions_and_rejects_publishing(self, bus: EventBus) -> None:
        stream = await bus.subscribe(SCOPE, ">")
        waiting = asyncio.create_task(stream.next())
        await asyncio.sleep(0)
        await bus.close()
        assert await asyncio.wait_for(waiting, self.patience) is None
        assert bus.closed
        with pytest.raises(BusClosedError):
            await bus.publish(make_event())

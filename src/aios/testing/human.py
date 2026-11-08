"""The approval and task store contracts (FR-HUM-01, 04 to 06, 08), for every backend.

An adapter passes by subclassing :class:`ApprovalStoreConformance` and giving it ``bus`` and
``store`` fixtures, and :class:`TaskStoreConformance` with ``bus`` and ``tasks``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import JsonValue

from aios.core.catalog import ApprovalRequested, HumanTaskAssigned
from aios.core.envelope import Event
from aios.core.human import Approval, HumanTask, Vote
from aios.core.ids import new_ulid
from aios.fabric import EventBus, Scope
from aios.human.store import ApprovalStore
from aios.human.tasks import TaskStore
from aios.testing import make_event

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
ALICE = "user:alice@acme"
LEAD = "user:lead@acme"


def request(
    *,
    tenant: str = "acme",
    run: str | None = None,
    approvers: tuple[str, ...] = ("role:lead",),
    expires_at: datetime = NOW + timedelta(hours=1),
    onbehalfof: str | None = ALICE,
    **details: Any,
) -> Event:
    """An ``approval.requested`` event for a refund."""
    payload = ApprovalRequested(
        **{  # pyright: ignore[reportArgumentType]
            "approval": new_ulid(),
            "approvers": list(approvers),
            "reason": "a policy asks for approval",
            "summary": 'pay.refund.create(amountUSD=120, order="1001")',
            "action": "pay.refund.create",
            "payload": {"amountUSD": 120, "order": "1001"},
            "risk": "high",
            "policies": ["refunds/approval/0"],
            "expires_at": expires_at,
            **details,
        }
    )
    return make_event(
        "approval.requested",
        payload.model_dump(mode="json"),
        tenant=tenant,
        app="desk",
        run_id=run or new_ulid(),
        onbehalfof=onbehalfof,
    )


def answer(approval: Approval, type: str = "approval.granted") -> Event:
    return approval.request.derive(
        type,
        {"approval": approval.id, "by": LEAD},
        source="aios://acme/system/human",
        principal=LEAD,
    )


class ApprovalStoreConformance:
    @pytest.fixture
    def bus(self) -> EventBus:
        raise NotImplementedError

    @pytest.fixture
    def store(self, bus: EventBus) -> ApprovalStore:
        raise NotImplementedError

    @staticmethod
    async def _added(store: ApprovalStore, **fields: Any) -> Approval:
        approval = Approval.of(request(**fields))
        await store.add(approval)
        return approval

    @staticmethod
    async def _settle(store: ApprovalStore, approval: Approval, **fields: Any) -> Approval | None:
        return await store.settle(
            approval,
            **{  # pyright: ignore[reportArgumentType]
                "status": "granted",
                "approved": True,
                "by": LEAD,
                "comment": "looks right",
                "at": NOW,
                "event": answer(approval),
                **fields,
            },
        )

    @staticmethod
    async def _published(bus: EventBus, run: str | None) -> list[str]:
        scope = Scope("*", "*")
        return [s.event.type async for s in bus.replay(scope, "approval.*", run_id=run)]

    async def test_a_new_request_is_pending(self, store: ApprovalStore) -> None:
        added = await self._added(store)
        found = await store.get("acme", added.id)
        assert found is not None
        assert (found.status, found.approved, found.by) == ("pending", False, None)
        assert found.details == added.details
        assert found.request == added.request

    async def test_keeping_a_request_again_changes_nothing(self, store: ApprovalStore) -> None:
        """Delivery is at least once, and a redelivery can arrive after the decision."""
        added = await self._added(store)
        assert await self._settle(store, added) is not None
        await store.add(added)
        found = await store.get("acme", added.id)
        assert found is not None
        assert found.status == "granted"

    async def test_an_unknown_approval_is_none(self, store: ApprovalStore) -> None:
        assert await store.get("acme", new_ulid()) is None

    async def test_settling_records_the_decision(self, store: ApprovalStore) -> None:
        added = await self._added(store)
        settled = await self._settle(store, added)
        assert settled is not None
        assert settled.status == "granted"
        assert settled.approved is True
        assert (settled.by, settled.comment, settled.decided_at) == (LEAD, "looks right", NOW)
        assert await store.get("acme", added.id) == settled

    async def test_settling_publishes_the_event(self, bus: EventBus, store: ApprovalStore) -> None:
        added = await self._added(store)
        await self._settle(store, added)
        assert await self._published(bus, added.run) == ["approval.granted"]

    async def test_only_the_first_answer_counts(self, bus: EventBus, store: ApprovalStore) -> None:
        added = await self._added(store)
        assert await self._settle(store, added) is not None
        late = await self._settle(
            store, added, status="denied", approved=False, event=answer(added, "approval.denied")
        )
        assert late is None
        found = await store.get("acme", added.id)
        assert found is not None
        assert found.status == "granted"
        assert await self._published(bus, added.run) == ["approval.granted"]

    async def test_of_many_answers_at_once_one_wins(
        self, bus: EventBus, store: ApprovalStore
    ) -> None:
        added = await self._added(store)
        answers = [self._settle(store, added, by=f"user:u{n}@acme") for n in range(8)]
        results = await asyncio.gather(*answers)
        assert len([result for result in results if result is not None]) == 1
        assert await self._published(bus, added.run) == ["approval.granted"]

    async def test_find_lists_by_status_oldest_first(self, store: ApprovalStore) -> None:
        first, second, closed = [await self._added(store) for _ in range(3)]
        await self._settle(store, closed)
        assert [a.id for a in await store.find("acme", "pending")] == [first.id, second.id]
        assert [a.id for a in await store.find("acme", "granted")] == [closed.id]

    async def test_find_respects_the_limit(self, store: ApprovalStore) -> None:
        for _ in range(3):
            await self._added(store)
        assert len(await store.find("acme", "pending", limit=2)) == 2

    async def test_tenants_keep_to_their_own(self, store: ApprovalStore) -> None:
        added = await self._added(store)
        assert await store.find("globex", "pending") == []
        assert await store.get("globex", added.id) is None
        assert added.run is not None
        assert await store.in_run("globex", added.run) == []

    async def test_a_runs_open_approvals_are_listed(self, store: ApprovalStore) -> None:
        run = new_ulid()
        pending = await self._added(store, run=run)
        closed = await self._added(store, run=run)
        await self._added(store)
        await self._settle(store, closed)
        assert [a.id for a in await store.in_run("acme", run)] == [pending.id]

    async def test_overdue_finds_what_is_past_its_time(self, store: ApprovalStore) -> None:
        due = await self._added(store, expires_at=NOW - timedelta(seconds=1))
        exactly = await self._added(store, tenant="globex", expires_at=NOW)
        await self._added(store, expires_at=NOW + timedelta(seconds=1))
        closed = await self._added(store, expires_at=NOW - timedelta(hours=1))
        await self._settle(store, closed)
        assert {a.id for a in await store.overdue(NOW)} == {due.id, exactly.id}

    async def test_overdue_respects_the_limit(self, store: ApprovalStore) -> None:
        for _ in range(3):
            await self._added(store, expires_at=NOW)
        assert len(await store.overdue(NOW, limit=2)) == 2

    # --------------------------------------------------------------- quorum, reminders, escalation

    @staticmethod
    def _event(approval: Approval, type: str = "approval.voted") -> Event:
        data: dict[str, Any] = {"approval": approval.id}
        if type == "approval.voted":
            data |= {"by": LEAD, "votes": 1, "quorum": max(approval.quorum, 2)}
        else:
            data |= {"count": 1}
        return approval.request.derive(type, data, source="test", principal=LEAD)

    async def test_votes_add_up_and_each_person_counts_once(
        self, bus: EventBus, store: ApprovalStore
    ) -> None:
        added = await self._added(store, quorum=3)
        first = await store.vote(added, Vote(LEAD, NOW, "fine"), event=self._event(added))
        assert first is not None
        assert first.voted == (LEAD,)
        assert await store.vote(added, Vote(LEAD, NOW), event=self._event(added)) is None
        second = await store.vote(first, Vote("user:sam@acme", NOW), event=self._event(added))
        assert second is not None
        assert second.voted == (LEAD, "user:sam@acme")
        found = await store.get("acme", added.id)
        assert found is not None
        assert found.votes == second.votes
        assert found.votes[0].comment == "fine"
        assert await self._published(bus, added.run) == ["approval.voted", "approval.voted"]

    async def test_a_closed_approval_takes_no_vote(self, store: ApprovalStore) -> None:
        added = await self._added(store, quorum=2)
        await self._settle(store, added, status="denied", approved=False)
        assert await store.vote(added, Vote(LEAD, NOW), event=self._event(added)) is None

    async def test_an_escalation_is_kept(self, store: ApprovalStore) -> None:
        added = await self._added(store, remind_every=timedelta(minutes=30))
        changed = Approval.of(added.request, progress={"level": 1, "reminders": 2})
        moved = await store.advance(added, changed, event=self._event(added, "approval.reminded"))
        assert moved is not None
        found = await store.get("acme", added.id)
        assert found is not None
        assert (found.level, found.reminders) == (1, 2)

    async def test_a_change_from_a_stale_read_changes_nothing(
        self, bus: EventBus, store: ApprovalStore
    ) -> None:
        """Two workers sweeping at once: one escalates, the other finds it already done."""
        added = await self._added(store, remind_every=timedelta(minutes=30))
        once = Approval.of(added.request, progress={"reminders": 1})
        assert await store.advance(added, once, event=self._event(added, "approval.reminded"))
        assert (
            await store.advance(added, once, event=self._event(added, "approval.reminded")) is None
        )
        assert await self._published(bus, added.run) == ["approval.reminded"]

    async def test_only_what_reminds_or_escalates_is_watched(self, store: ApprovalStore) -> None:
        await self._added(store)
        reminds = await self._added(store, remind_every=timedelta(minutes=30))
        escalates = await self._added(
            store,
            escalation=[{"after": timedelta(hours=1), "approvers": ["role:manager"]}],
        )
        watched = {approval.id for approval in await store.waiting()}
        assert watched == {reminds.id, escalates.id}

    async def test_an_approvers_edits_are_kept(self, store: ApprovalStore) -> None:
        added = await self._added(store)
        settled = await self._settle(store, added, edits={"amountUSD": 60})
        assert settled is not None
        assert settled.edits == {"amountUSD": 60}
        found = await store.get("acme", added.id)
        assert found is not None
        assert found.edits == {"amountUSD": 60}


# ---------------------------------------------------------------------- work for people


def task_request(*, tenant: str = "acme", run: str | None = None) -> Event:
    """A ``task.requested`` event for a capability people provide."""
    run = run or new_ulid()
    return make_event(
        "task.requested",
        {"capability": "legal.review", "input": {"contract": "nda-7"}, "step": "review"},
        tenant=tenant,
        app="desk",
        run_id=run,
        principal="system:workflow",
    )


def assigned_task(request_event: Event) -> HumanTask:
    details = HumanTaskAssigned(
        task=request_event.id,
        capability="legal.review",
        assignees=["group:legal"],
        input={"contract": "nda-7"},
        step="review",
    )
    return HumanTask(request_event, details)


class TaskStoreConformance:
    """The store of work for people (FR-HUM-08): one suite for every backend."""

    @pytest.fixture
    def bus(self) -> EventBus:
        raise NotImplementedError

    @pytest.fixture
    def tasks(self, bus: EventBus) -> TaskStore:
        raise NotImplementedError

    @staticmethod
    def _assigned(task: HumanTask) -> list[Event]:
        data = task.details.model_dump(mode="json")
        return [task.request.derive("human.task.assigned", data, source="test", principal=LEAD)]

    @staticmethod
    def _done(task: HumanTask) -> list[Event]:
        data: dict[str, JsonValue] = {"task": task.id, "by": LEAD, "output": {"verdict": "ok"}}
        return [task.request.derive("human.task.completed", data, source="test", principal=LEAD)]

    @staticmethod
    async def _published(bus: EventBus, run: str | None) -> list[str]:
        scope = Scope("*", "*")
        return [s.event.type async for s in bus.replay(scope, "human.task.*", run_id=run)]

    async def test_a_task_is_kept_with_its_event_once(
        self, bus: EventBus, tasks: TaskStore
    ) -> None:
        task = assigned_task(task_request())
        assert await tasks.add(task, events=self._assigned(task))
        assert not await tasks.add(task, events=self._assigned(task))  # a redelivery
        assert await self._published(bus, task.run) == ["human.task.assigned"]
        found = await tasks.get("acme", task.id)
        assert found is not None
        assert (found.status, found.details, found.request) == (
            "pending",
            task.details,
            task.request,
        )

    async def test_the_first_to_finish_it_wins(self, bus: EventBus, tasks: TaskStore) -> None:
        task = assigned_task(task_request())
        await tasks.add(task, events=self._assigned(task))

        async def finish() -> HumanTask | None:
            return await tasks.close(
                task,
                status="completed",
                by=LEAD,
                output={"verdict": "ok"},
                comment="fine",
                at=NOW,
                events=self._done(task),
            )

        results = await asyncio.gather(finish(), finish(), finish())
        assert sum(r is not None for r in results) == 1
        published = await self._published(bus, task.run)
        assert published == ["human.task.assigned", "human.task.completed"]
        found = await tasks.get("acme", task.id)
        assert found is not None
        assert (found.status, found.by, found.output, found.comment) == (
            "completed",
            LEAD,
            {"verdict": "ok"},
            "fine",
        )
        assert found.completed_at == NOW

    async def test_open_work_by_tenant_and_by_run(self, tasks: TaskStore) -> None:
        run = new_ulid()
        mine = assigned_task(task_request(run=run))
        other_run = assigned_task(task_request())
        elsewhere = assigned_task(task_request(tenant="globex"))
        for task in (mine, other_run, elsewhere):
            await tasks.add(task, events=self._assigned(task))
        assert {t.id for t in await tasks.pending("acme")} == {mine.id, other_run.id}
        assert [t.id for t in await tasks.in_run("acme", run)] == [mine.id]
        await tasks.close(
            mine, status="cancelled", by=LEAD, output=None, comment=None, at=NOW, events=[]
        )
        assert [t.id for t in await tasks.pending("acme")] == [other_run.id]
        assert await tasks.in_run("acme", run) == []
        assert await tasks.get("acme", elsewhere.id) is None  # another tenant's

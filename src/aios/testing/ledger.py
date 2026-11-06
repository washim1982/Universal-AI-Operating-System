"""The spend ledger contract: what every ledger must do, on memory and on PostgreSQL (ADR-018)."""

from __future__ import annotations

import asyncio

from aios.core.envelope import Event
from aios.fabric import EventBus, Scope
from aios.models.ledger import SpendLedger

TENANT = "acme"
OTHER = "globex"
RUN = "01HZY0000000000000000000R1"


def _notice(tenant: str = TENANT) -> Event:
    return Event.model_validate(
        {
            "type": "budget.warning",
            "source": f"aios://{tenant}/system/models",
            "tenantid": tenant,
            "appid": "desk",
            "runid": RUN,
            "principal": "system:models",
            "data": {
                "level": "tenant",
                "limit": 10.0,
                "soft_limit": 8.0,
                "observed": 8.0,
                "action": "warn",
            },
        }
    )


class SpendLedgerConformance:
    """Subclass with ``bus`` and ``store`` fixtures."""

    async def _published(self, bus: EventBus, tenant: str = TENANT) -> list[Event]:
        return [stored.event async for stored in bus.replay(Scope(tenant, "desk"), ">")]

    async def test_nothing_spent_is_left_out(self, store: SpendLedger) -> None:
        assert await store.spent(TENANT, ["tenant:2026-11"]) == {}

    async def test_a_charge_adds_up_for_everyone_paying(
        self, bus: EventBus, store: SpendLedger
    ) -> None:
        subjects = ["tenant:2026-11", "app:desk:2026-11"]
        await store.charge(TENANT, subjects, 1.5, _notice())
        await store.charge(TENANT, subjects[:1], 0.25, _notice())
        assert await store.spent(TENANT, subjects) == {
            "tenant:2026-11": 1.75,
            "app:desk:2026-11": 1.5,
        }

    async def test_a_charge_publishes_what_it_paid_for(
        self, bus: EventBus, store: SpendLedger
    ) -> None:
        event = _notice()
        await store.charge(TENANT, ["tenant:2026-11"], 1.0, event)
        assert [e.id for e in await self._published(bus)] == [event.id]

    async def test_a_free_call_is_published_and_costs_nothing(
        self, bus: EventBus, store: SpendLedger
    ) -> None:
        event = _notice()
        await store.charge(TENANT, ["tenant:2026-11"], 0.0, event)
        assert [e.id for e in await self._published(bus)] == [event.id]
        assert await store.spent(TENANT, ["tenant:2026-11"]) == {}

    async def test_tenants_pay_separately(self, store: SpendLedger) -> None:
        await store.charge(TENANT, ["tenant:2026-11"], 1.0, _notice())
        assert await store.spent(OTHER, ["tenant:2026-11"]) == {}

    async def test_only_the_subjects_asked_for_come_back(self, store: SpendLedger) -> None:
        await store.charge(TENANT, ["tenant:2026-11", "tenant:2026-10"], 1.0, _notice())
        assert await store.spent(TENANT, ["tenant:2026-11"]) == {"tenant:2026-11": 1.0}

    async def test_charges_at_the_same_time_all_count(self, store: SpendLedger) -> None:
        subjects = ["tenant:2026-11", "app:desk:2026-11", f"run:{RUN}"]
        orders = (subjects, subjects[::-1])  # in opposite orders, two charges could deadlock
        await asyncio.gather(
            *(store.charge(TENANT, orders[n % 2], 0.5, _notice()) for n in range(20))
        )
        assert await store.spent(TENANT, subjects) == dict.fromkeys(subjects, 10.0)

    async def test_a_warning_goes_out_once(self, bus: EventBus, store: SpendLedger) -> None:
        first = _notice()
        assert await store.warn_once(TENANT, "tenant:2026-11", first) is True
        assert await store.warn_once(TENANT, "tenant:2026-11", _notice()) is False
        assert [e.id for e in await self._published(bus)] == [first.id]

    async def test_a_warning_goes_out_once_at_the_same_time_too(
        self, bus: EventBus, store: SpendLedger
    ) -> None:
        answers = await asyncio.gather(
            *(store.warn_once(TENANT, "tenant:2026-11", _notice()) for _ in range(8))
        )
        assert sorted(answers) == [False] * 7 + [True]
        assert len(await self._published(bus)) == 1

    async def test_a_warning_after_spending_keeps_the_total(self, store: SpendLedger) -> None:
        await store.charge(TENANT, ["tenant:2026-11"], 2.0, _notice())
        await store.warn_once(TENANT, "tenant:2026-11", _notice())
        assert await store.spent(TENANT, ["tenant:2026-11"]) == {"tenant:2026-11": 2.0}

    async def test_a_warning_before_spending_doesnt_get_in_the_way(
        self, store: SpendLedger
    ) -> None:
        await store.warn_once(TENANT, "tenant:2026-11", _notice())
        assert await store.spent(TENANT, ["tenant:2026-11"]) == {}
        await store.charge(TENANT, ["tenant:2026-11"], 2.0, _notice())
        assert await store.spent(TENANT, ["tenant:2026-11"]) == {"tenant:2026-11": 2.0}

    async def test_each_subject_is_warned_for_itself(self, store: SpendLedger) -> None:
        assert await store.warn_once(TENANT, "tenant:2026-11", _notice()) is True
        assert await store.warn_once(TENANT, "tenant:2026-12", _notice()) is True
        assert await store.warn_once(OTHER, "tenant:2026-11", _notice(OTHER)) is True

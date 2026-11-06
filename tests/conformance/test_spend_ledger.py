"""The spend ledger contract, on memory and, with ``AIOS_TEST_DATABASE_URL``, on PostgreSQL."""

import os
from collections.abc import AsyncIterator

import pytest

from aios.core.ids import new_ulid
from aios.fabric import EventBus, InMemoryEventBus
from aios.fabric.postgres import PostgresEventBus
from aios.models import ledger_for
from aios.models.ledger import InMemorySpendLedger, SpendLedger
from aios.models.postgres import PostgresSpendLedger
from aios.testing.ledger import SpendLedgerConformance
from aios.testing.runtime import LocalRuntime

DSN = os.environ.get("AIOS_TEST_DATABASE_URL")


async def test_a_fabric_gets_the_ledger_it_can_share() -> None:
    async with InMemoryEventBus() as bus:
        assert isinstance(ledger_for(bus), InMemorySpendLedger)


@pytest.mark.skipif(DSN is None, reason="set AIOS_TEST_DATABASE_URL to run")
async def test_a_runtime_on_postgresql_keeps_its_totals_there() -> None:
    async with LocalRuntime(durable=True) as runtime:
        assert isinstance(runtime.runtime.models.ledger, PostgresSpendLedger)


class TestInMemoryLedger(SpendLedgerConformance):
    @pytest.fixture
    async def bus(self) -> AsyncIterator[EventBus]:
        async with InMemoryEventBus() as bus:
            yield bus

    @pytest.fixture
    def store(self, bus: EventBus) -> SpendLedger:
        return InMemorySpendLedger(bus)


@pytest.mark.skipif(DSN is None, reason="set AIOS_TEST_DATABASE_URL to run")
class TestPostgresLedger(SpendLedgerConformance):
    @pytest.fixture
    async def bus(self) -> AsyncIterator[EventBus]:
        assert DSN is not None
        async with PostgresEventBus(
            DSN, schema=f"t_{new_ulid().lower()}", drop_on_close=True
        ) as bus:
            yield bus

    @pytest.fixture
    def store(self, bus: EventBus) -> SpendLedger:
        assert isinstance(bus, PostgresEventBus)
        return PostgresSpendLedger(bus)

"""The approval store contract, on memory and, with ``AIOS_TEST_DATABASE_URL``, on PostgreSQL."""

import os
from collections.abc import AsyncIterator

import pytest

from aios.core.ids import new_ulid
from aios.fabric import EventBus, InMemoryEventBus
from aios.fabric.postgres import PostgresEventBus
from aios.human import ApprovalStore, InMemoryApprovalStore
from aios.human.postgres import PostgresApprovalStore
from aios.testing.human import ApprovalStoreConformance

DSN = os.environ.get("AIOS_TEST_DATABASE_URL")


class TestInMemoryStore(ApprovalStoreConformance):
    @pytest.fixture
    async def bus(self) -> AsyncIterator[EventBus]:
        async with InMemoryEventBus() as bus:
            yield bus

    @pytest.fixture
    def store(self, bus: EventBus) -> ApprovalStore:
        return InMemoryApprovalStore(bus)


@pytest.mark.skipif(DSN is None, reason="set AIOS_TEST_DATABASE_URL to run")
class TestPostgresStore(ApprovalStoreConformance):
    @pytest.fixture
    async def bus(self) -> AsyncIterator[EventBus]:
        assert DSN is not None
        async with PostgresEventBus(
            DSN, schema=f"t_{new_ulid().lower()}", drop_on_close=True
        ) as bus:
            yield bus

    @pytest.fixture
    def store(self, bus: EventBus) -> ApprovalStore:
        assert isinstance(bus, PostgresEventBus)
        return PostgresApprovalStore(bus)

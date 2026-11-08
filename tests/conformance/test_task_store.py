"""The store of work for people, on memory and, with ``AIOS_TEST_DATABASE_URL``, PostgreSQL."""

import os
from collections.abc import AsyncIterator

import pytest

from aios.core.ids import new_ulid
from aios.fabric import EventBus, InMemoryEventBus
from aios.fabric.postgres import PostgresEventBus
from aios.human import InMemoryTaskStore, TaskStore
from aios.human.postgres import PostgresTaskStore
from aios.testing.human import TaskStoreConformance

DSN = os.environ.get("AIOS_TEST_DATABASE_URL")


class TestInMemoryTasks(TaskStoreConformance):
    @pytest.fixture
    async def bus(self) -> AsyncIterator[EventBus]:
        async with InMemoryEventBus() as bus:
            yield bus

    @pytest.fixture
    def tasks(self, bus: EventBus) -> TaskStore:
        return InMemoryTaskStore(bus)


@pytest.mark.skipif(DSN is None, reason="set AIOS_TEST_DATABASE_URL to run")
class TestPostgresTasks(TaskStoreConformance):
    @pytest.fixture
    async def bus(self) -> AsyncIterator[EventBus]:
        assert DSN is not None
        async with PostgresEventBus(
            DSN, schema=f"t_{new_ulid().lower()}", drop_on_close=True
        ) as bus:
            yield bus

    @pytest.fixture
    def tasks(self, bus: EventBus) -> TaskStore:
        assert isinstance(bus, PostgresEventBus)
        return PostgresTaskStore(bus)

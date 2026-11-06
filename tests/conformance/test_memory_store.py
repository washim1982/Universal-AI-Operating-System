"""The memory store contract, on memory and, with ``AIOS_TEST_DATABASE_URL``, on pgvector."""

import os
from collections.abc import AsyncIterator

import pytest

from aios.core.ids import new_ulid
from aios.fabric.postgres import PostgresEventBus
from aios.memory.postgres import PostgresMemoryStore
from aios.memory.store import InMemoryMemoryStore, MemoryStore
from aios.testing.memory import MemoryStoreConformance

DSN = os.environ.get("AIOS_TEST_DATABASE_URL")


class TestInMemoryStore(MemoryStoreConformance):
    @pytest.fixture
    def store(self) -> MemoryStore:
        return InMemoryMemoryStore()


@pytest.mark.skipif(DSN is None, reason="set AIOS_TEST_DATABASE_URL to run")
class TestPostgresStore(MemoryStoreConformance):
    @pytest.fixture
    async def store(self) -> AsyncIterator[MemoryStore]:
        assert DSN is not None
        schema = f"t_{new_ulid().lower()}"
        async with PostgresEventBus(DSN, schema=schema, drop_on_close=True) as bus:
            yield PostgresMemoryStore(bus)

"""The installation store contract, on memory and, with ``AIOS_TEST_DATABASE_URL``, PostgreSQL."""

import os
from collections.abc import AsyncIterator

import pytest

from aios.controlplane.installations import InMemoryInstallationStore, InstallationStore
from aios.controlplane.postgres import PostgresInstallationStore
from aios.core.ids import new_ulid
from aios.fabric import EventBus, InMemoryEventBus
from aios.fabric.postgres import PostgresEventBus
from aios.testing.installations import InstallationStoreConformance
from aios.testing.runtime import LocalRuntime

DSN = os.environ.get("AIOS_TEST_DATABASE_URL")


class TestInMemoryStore(InstallationStoreConformance):
    @pytest.fixture
    async def bus(self) -> AsyncIterator[EventBus]:
        async with InMemoryEventBus() as bus:
            yield bus

    @pytest.fixture
    def store(self, bus: EventBus) -> InstallationStore:
        return InMemoryInstallationStore(bus)


@pytest.mark.skipif(DSN is None, reason="set AIOS_TEST_DATABASE_URL to run")
class TestPostgresStore(InstallationStoreConformance):
    @pytest.fixture
    async def bus(self) -> AsyncIterator[EventBus]:
        assert DSN is not None
        async with PostgresEventBus(
            DSN, schema=f"t_{new_ulid().lower()}", drop_on_close=True
        ) as bus:
            yield bus

    @pytest.fixture
    def store(self, bus: EventBus) -> InstallationStore:
        assert isinstance(bus, PostgresEventBus)
        return PostgresInstallationStore(bus)


@pytest.mark.skipif(DSN is None, reason="set AIOS_TEST_DATABASE_URL to run")
async def test_a_runtime_on_postgresql_keeps_its_installations_there() -> None:
    async with LocalRuntime(durable=True) as runtime:
        assert isinstance(runtime.runtime.installations, PostgresInstallationStore)

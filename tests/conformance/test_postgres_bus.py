"""The shared EventBus contract against PostgreSQL (WP 1.1, ADR-004).

Runs when ``AIOS_TEST_DATABASE_URL`` points at a PostgreSQL 17 database (CI provides one);
each test gets its own schema, dropped afterwards.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# (asyncpg ships no type hints)

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from aios.core.ids import new_ulid
from aios.fabric.port import EventBus, Scope
from aios.fabric.postgres import PostgresEventBus
from aios.testing import make_event
from aios.testing.fabric import EventBusConformance

DSN = os.environ.get("AIOS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(DSN is None, reason="set AIOS_TEST_DATABASE_URL to run")


class TestPostgresEventBus(EventBusConformance):
    patience = 5.0

    @pytest.fixture
    async def bus(self) -> AsyncIterator[EventBus]:
        assert DSN is not None
        bus = PostgresEventBus(DSN, schema=f"t_{new_ulid().lower()}", drop_on_close=True)
        await bus.connect()
        try:
            yield bus
        finally:
            await bus.close()


async def _publish_then_fail(bus: PostgresEventBus, connection: Any) -> None:
    """A state change that fails after publishing: its transaction rolls back."""
    try:
        async with connection.transaction():
            await bus.publish_in(connection, make_event("order.item.created"))
            raise RuntimeError("the state change failed")
    except RuntimeError:
        return
    raise AssertionError("the transaction should have failed")


async def test_publish_in_commits_with_the_callers_transaction() -> None:
    """The transactional outbox (FR-EVT-07): events commit or roll back with the state change."""
    assert DSN is not None
    async with PostgresEventBus(DSN, schema=f"t_{new_ulid().lower()}", drop_on_close=True) as bus:
        scope = Scope("acme", "ai-researcher")
        async with bus.pool.acquire() as connection:
            await _publish_then_fail(bus, connection)
            async with connection.transaction():
                await bus.publish_in(connection, kept := make_event("order.item.created"))
        assert [s.event.id async for s in bus.replay(scope)] == [kept.id]

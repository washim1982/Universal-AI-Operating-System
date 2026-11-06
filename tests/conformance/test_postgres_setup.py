"""Processes set up the PostgreSQL schema safely together, and while others work.

Every process sets up the schema when it connects, and each store when it is first used: an API
and its workers start at once, and a worker restarts while the API serves traffic. Runs when
``AIOS_TEST_DATABASE_URL`` is set; each test gets its own schema, dropped afterwards.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# (asyncpg ships no type hints)

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import pytest

from aios.controlplane.postgres import PostgresInstallationStore
from aios.core.ids import new_ulid
from aios.fabric.port import Scope
from aios.fabric.postgres import PostgresEventBus
from aios.human.postgres import PostgresApprovalStore
from aios.memory.postgres import PostgresMemoryStore
from aios.models.postgres import PostgresSpendLedger
from aios.security.postgres import PostgresAuditLog, PostgresSecurityStore
from aios.testing import DEFAULT_APP, DEFAULT_TENANT, make_event

DSN = os.environ.get("AIOS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(DSN is None, reason="set AIOS_TEST_DATABASE_URL to run")


@pytest.fixture
async def schema() -> AsyncIterator[str]:
    assert DSN is not None
    name = f"t_{new_ulid().lower()}"
    yield name
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
    finally:
        await connection.close()


async def _set_up(bus: PostgresEventBus) -> None:
    """What a process does when it starts: connect, then use every store, which sets it up."""
    await bus.connect()
    now = datetime.now(UTC)
    await asyncio.gather(
        PostgresInstallationStore(bus).everywhere(),
        PostgresApprovalStore(bus).overdue(now),
        PostgresSpendLedger(bus).spent(DEFAULT_TENANT, ["x"]),
        PostgresMemoryStore(bus).expire(now),
        PostgresSecurityStore(bus).roles(DEFAULT_TENANT, "user:alice@acme"),
        PostgresAuditLog(bus).entries(DEFAULT_TENANT),
    )


async def _start(schema: str) -> PostgresEventBus:
    assert DSN is not None
    bus = PostgresEventBus(DSN, schema=schema)
    try:
        await _set_up(bus)
    except BaseException:
        await bus.close()
        raise
    return bus


async def test_every_process_can_set_up_at_once_on_a_new_schema(schema: str) -> None:
    """Several processes start together, and each sets up the schema and every store."""
    started = await asyncio.gather(*(_start(schema) for _ in range(6)), return_exceptions=True)
    for bus in started:
        if isinstance(bus, PostgresEventBus):
            await bus.close()
    assert [error for error in started if isinstance(error, BaseException)] == []


async def _publish(bus: PostgresEventBus, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await bus.publish(*(make_event("order.item.created") for _ in range(10)))
        await asyncio.sleep(0)


async def _consume(bus: PostgresEventBus, stop: asyncio.Event, handled: list[str]) -> None:
    subscription = await bus.subscribe(Scope(DEFAULT_TENANT, DEFAULT_APP), "order.>", group="w")
    try:
        while not stop.is_set():
            delivery = await subscription.next(timeout=0.05)
            if delivery is not None:
                handled.append(delivery.event.id)
                await delivery.ack()
    finally:
        await subscription.close()


async def test_a_process_can_start_while_others_work(schema: str) -> None:
    """A worker restarts while the API publishes and other workers consume, again and again.

    A setup that takes table locks waits behind that traffic while holding locks it needs: one of
    them would stop with ``deadlock detected``.
    """
    working = await _start(schema)
    stop = asyncio.Event()
    handled: list[str] = []
    traffic = [
        asyncio.create_task(_publish(working, stop)),
        *(asyncio.create_task(_consume(working, stop, handled)) for _ in range(3)),
    ]
    restarts: list[BaseException] = []
    try:
        await asyncio.sleep(0.3)
        for _ in range(8):
            try:
                await (await _start(schema)).close()
            except Exception as error:  # every failure is reported below
                restarts.append(error)
            if any(task.done() for task in traffic):
                break
    finally:
        stop.set()
        outcomes = await asyncio.gather(*traffic, return_exceptions=True)
        await working.close()
    assert restarts == []
    assert [error for error in outcomes if isinstance(error, BaseException)] == []
    assert handled


async def test_starting_takes_no_lock_that_traffic_holds_up(schema: str) -> None:
    """Once the schema is set up, a process starting takes no lock a writer would block."""
    assert DSN is not None
    await (await _start(schema)).close()
    writer = await asyncpg.connect(DSN)
    starting = PostgresEventBus(DSN, schema=schema)
    try:
        async with writer.transaction():
            tables = await writer.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = $1", schema
            )
            names = ", ".join(f"{schema}.{row['tablename']}" for row in tables)
            await writer.execute(f"LOCK TABLE {names} IN ROW EXCLUSIVE MODE")
            await asyncio.wait_for(_set_up(starting), timeout=5)
    finally:
        with contextlib.suppress(Exception):
            await starting.close()
        await writer.close()


async def test_a_changed_setup_gives_way_to_traffic_until_it_can_run(
    schema: str, caplog: pytest.LogCaptureFixture
) -> None:
    """An upgrade's new setup text waits for a busy table without queueing writers behind it."""
    assert DSN is not None
    caplog.set_level(logging.INFO, logger="aios.fabric.postgres")
    bus = await _start(schema)
    writer = await asyncpg.connect(DSN)
    try:
        async with writer.transaction():
            await writer.execute(bus.sql("LOCK TABLE {s}.events IN ROW EXCLUSIVE MODE"))
            upgrade = asyncio.create_task(
                bus.set_up("probe", bus.sql("CREATE INDEX IF NOT EXISTS probe ON {s}.events (id);"))
            )
            await asyncio.sleep(1.5)
            assert not upgrade.done()
        await asyncio.wait_for(upgrade, timeout=5)
    finally:
        await writer.close()
        await bus.close()
    assert "trying again" in caplog.text

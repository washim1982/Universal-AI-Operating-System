"""PostgreSQL itself keeps tenants apart (FR-SEC-06): these tests leave out every ``WHERE tenant``.

The queries run as a tenant connection would, so a filter forgotten in the code above can't leak.
Runs when ``AIOS_TEST_DATABASE_URL`` is set.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# (asyncpg ships no type hints)

import contextlib
import os
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from aios.core.human import Approval
from aios.core.identity import ApiKey, Suspendable, Suspension
from aios.core.ids import new_ulid
from aios.core.memory import GLOBAL_OWNER, GLOBAL_TENANT
from aios.fabric import Scope
from aios.fabric.postgres import TENANT_ROLE, PostgresEventBus
from aios.human.postgres import PostgresApprovalStore
from aios.memory.postgres import PostgresMemoryStore
from aios.memory.store import Area, Stored
from aios.security.audit import InMemoryAuditLog
from aios.security.postgres import PostgresAuditLog, PostgresSecurityStore
from aios.testing import make_event
from aios.testing.human import LEAD, answer, request
from aios.testing.memory import item, vector

DSN = os.environ.get("AIOS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(DSN is None, reason="set AIOS_TEST_DATABASE_URL to run")


@pytest.fixture
async def bus() -> AsyncIterator[PostgresEventBus]:
    assert DSN is not None
    schema = f"t_{new_ulid().lower()}"
    async with PostgresEventBus(DSN, schema=schema, drop_on_close=True) as bus:
        yield bus


async def _seed(bus: PostgresEventBus) -> None:
    store, audit = PostgresSecurityStore(bus), PostgresAuditLog(bus)
    now = datetime.now(UTC)
    for tenant in ("acme", "globex"):
        event = make_event("order.item.created", tenantid=tenant)
        await bus.publish(event)
        await audit.append(make_event("security.key.created", tenantid=tenant))
        await store.add_key(
            ApiKey(
                id=new_ulid(),
                tenant=tenant,
                name="ci",
                principal=f"service:ci@{tenant}",
                roles=frozenset({"member"}),
                digest="d" * 64,
                created_at=now,
                created_by="system:test",
            )
        )
        await store.set_roles(tenant, f"user:alice@{tenant}", frozenset({"member"}))
        await store.suspend(Suspension(tenant, Suspendable.AGENT, "x", "test", "system:test", now))
    async for _ in bus.replay(Scope.platform()):
        pass  # every event now has its position


async def _tables(bus: PostgresEventBus) -> list[str]:
    async with bus.pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = $1", bus.schema
        )
    return sorted(row["tablename"] for row in rows)


async def _seed_memory(bus: PostgresEventBus) -> PostgresMemoryStore:
    store = PostgresMemoryStore(bus)
    for tenant in ("acme", "globex"):
        await store.add([Stored(item(f"{tenant} refund", tenant=tenant), vector(1.0, 0.0))])
    return store


@pytest.mark.parametrize(
    "query",
    [
        "SELECT tenantid FROM {s}.memory_items",
        "SELECT tenantid FROM {s}.memory_items WHERE search @@ to_tsquery('refund')",
        "SELECT tenantid FROM {s}.memory_items ORDER BY embedding <=> '[1,0]'::vector LIMIT 10",
    ],
    ids=["no-filter", "full-text", "vector"],
)
async def test_memory_searches_with_no_filter_see_one_tenant(
    bus: PostgresEventBus, query: str
) -> None:
    """Similarity search itself can't cross tenants, whatever the code above it does."""
    await _seed_memory(bus)
    for tenant in ("acme", "globex"):
        async with bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(bus.sql(query))
        assert {row["tenantid"] for row in rows} == {tenant}


NOW = datetime.now(UTC)
EVERY_APP = [Area("app")]
SEARCH: dict[str, Any] = {"limit": 10, "filters": {}, "now": NOW}


async def _memory_reads(store: PostgresMemoryStore, tenant: str) -> list[object]:
    """Every read a memory store offers, for this tenant."""
    return [
        await store.keyword(tenant, EVERY_APP, "refund", **SEARCH),
        await store.nearest(tenant, EVERY_APP, vector(1.0, 0.0), **SEARCH),
        await store.any_in(tenant, EVERY_APP, now=NOW),
        await store.documents(tenant, Area("app", "desk"), ""),
    ]


async def test_memory_reads_really_go_through_a_tenant_connection(
    bus: PostgresEventBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queries name the tenant too, so their results alone can't show that RLS is in use."""
    store = await _seed_memory(bus)
    bound: list[str] = []
    real = bus.tenant_connection

    def spy(tenant: str) -> Any:
        bound.append(tenant)
        return real(tenant)

    monkeypatch.setattr(bus, "tenant_connection", spy)
    await _memory_reads(store, "acme")
    await store.get("acme", new_ulid())
    assert bound == ["acme"] * 5


async def test_memory_queries_name_the_tenant_as_well(
    bus: PostgresEventBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If row-level security ever failed, the query would still hold: each covers for the other."""
    store = await _seed_memory(bus)
    theirs = [item("acme only", tenant="acme", document="kb/a.md", document_hash="h")]
    await store.add([Stored(i) for i in theirs])

    @contextlib.asynccontextmanager
    async def unprotected(tenant: str) -> AsyncGenerator[Any]:
        async with bus.pool.acquire() as connection, connection.transaction():
            yield connection

    monkeypatch.setattr(bus, "tenant_connection", unprotected)
    hits = await store.keyword("globex", EVERY_APP, "refund acme", **SEARCH)
    assert {hit.item.tenant for hit in hits} == {"globex"}
    near = await store.nearest("globex", EVERY_APP, vector(1.0, 0.0), **SEARCH)
    assert {hit.item.tenant for hit in near} == {"globex"}
    assert await store.get("globex", theirs[0].id) is None


async def test_global_memory_is_the_one_thing_tenants_share(bus: PostgresEventBus) -> None:
    store = await _seed_memory(bus)
    shared = item("public law", tenant=GLOBAL_TENANT, scope="global", owner=GLOBAL_OWNER)
    await store.add([Stored(shared)])
    async with bus.tenant_connection("globex") as connection:
        rows = await connection.fetch(
            f"SELECT tenantid FROM {bus.schema}.memory_items"  # noqa: S608
        )
    assert {row["tenantid"] for row in rows} == {"globex", GLOBAL_TENANT}


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO {s}.memory_items (tenantid) VALUES ('globex')",
        "UPDATE {s}.memory_items SET content = 'x'",
        "DELETE FROM {s}.memory_items",
    ],
)
async def test_a_tenant_connection_can_not_write_memory(
    bus: PostgresEventBus, statement: str
) -> None:
    await _seed_memory(bus)
    async with bus.tenant_connection("acme") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(bus.sql(statement))


async def test_erasure_leaves_no_row_and_so_no_vector_behind(bus: PostgresEventBus) -> None:
    """Vectors live in the item's row: there is no second copy to forget (ADR-009)."""
    store = PostgresMemoryStore(bus)
    gone = item("a person's details", subject="user:alice@acme")
    await store.add([Stored(gone, vector(1.0, 0.0))])
    await store.erase("acme", subject="user:alice@acme", now=datetime.now(UTC))
    async with bus.pool.acquire() as connection:
        count = await connection.fetchval(
            f"SELECT count(*) FROM {bus.schema}.memory_items"  # noqa: S608
        )
    assert count == 0


@pytest.mark.parametrize(
    "table", ["events", "audit_log", "audit_heads", "suspensions", "principals"]
)
async def test_a_query_with_no_filter_sees_one_tenant(bus: PostgresEventBus, table: str) -> None:
    await _seed(bus)
    for tenant in ("acme", "globex"):
        async with bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(f"SELECT tenantid FROM {bus.schema}.{table}")  # noqa: S608
        assert {row["tenantid"] for row in rows} == {tenant}


async def test_tenant_reads_really_go_through_a_tenant_connection(
    bus: PostgresEventBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay filters by tenant in SQL as well, so results alone can't show that RLS is in use."""
    await _seed(bus)
    bound: list[str] = []
    real = bus.tenant_connection

    def spy(tenant: str) -> Any:
        bound.append(tenant)
        return real(tenant)

    monkeypatch.setattr(bus, "tenant_connection", spy)
    assert {s.event.tenantid async for s in bus.replay(Scope("acme"))} == {"acme"}
    assert await bus.dead_letters(Scope("globex")) == []
    assert bound == ["acme", "globex"]
    bound.clear()
    assert {s.event.tenantid async for s in bus.replay(Scope.platform())} == {"acme", "globex"}
    assert bound == []


async def test_a_wildcard_can_not_open_a_tenant_connection(bus: PostgresEventBus) -> None:
    with pytest.raises(ValueError, match="one tenant"):
        async with bus.tenant_connection("*"):
            pass


async def test_tenants_alternate_safely_on_one_connection(bus: PostgresEventBus) -> None:
    """A plan cached for one tenant must not serve the next one."""
    await _seed(bus)
    query = f"SELECT tenantid FROM {bus.schema}.events"  # noqa: S608
    async with bus.pool.acquire() as connection:
        for tenant in ("acme", "globex", "acme", "globex", "acme"):
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('role', $1, true), set_config('aios.tenant', $2, true)",
                    TENANT_ROLE,
                    tenant,
                )
                rows = await connection.fetch(query)
            assert {row["tenantid"] for row in rows} == {tenant}
        assert len(await connection.fetch(query)) == 2  # the connection is back to the platform


async def test_no_tenant_set_means_no_rows(bus: PostgresEventBus) -> None:
    await _seed(bus)
    async with bus.pool.acquire() as connection, connection.transaction():
        await connection.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
        assert await connection.fetch(f"SELECT 1 FROM {bus.schema}.events") == []  # noqa: S608


@pytest.mark.parametrize("table", ["groups", "deliveries", "sequencer"])
async def test_delivery_bookkeeping_has_no_tenant_view(bus: PostgresEventBus, table: str) -> None:
    await _seed(bus)
    async with bus.tenant_connection("acme") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch(f"SELECT 1 FROM {bus.schema}.{table}")  # noqa: S608


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO {s}.events (id, type, subject, tenantid, appid, time, body) "
        "VALUES ('x', 'a.b', 'a', 'acme', 'a', now(), '{{}}')",
        "UPDATE {s}.events SET tenantid = 'acme'",
        "DELETE FROM {s}.events",
        "UPDATE {s}.audit_log SET hash = 'x'",
        "DELETE FROM {s}.suspensions",
        "UPDATE {s}.api_keys SET revoked_at = NULL",
    ],
)
async def test_a_tenant_connection_can_not_write(bus: PostgresEventBus, statement: str) -> None:
    await _seed(bus)
    async with bus.tenant_connection("acme") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(statement.format(s=bus.schema))


async def test_a_tenant_connection_can_not_read_key_digests(bus: PostgresEventBus) -> None:
    await _seed(bus)
    async with bus.tenant_connection("acme") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch(f"SELECT digest FROM {bus.schema}.api_keys")  # noqa: S608


async def test_every_table_is_covered(bus: PostgresEventBus) -> None:
    """A table added later fails here until someone decides what tenants may see in it."""
    await _seed(bus)
    await _seed_memory(bus)
    await _seed_approvals(bus)
    tables = await _tables(bus)
    assert "memory_items" in tables
    assert "approvals" in tables
    async with bus.pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT relname, relrowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relkind = 'r'",
            bus.schema,
        )
    assert sorted(row["relname"] for row in rows) == tables
    unprotected = [row["relname"] for row in rows if not row["relrowsecurity"]]
    assert unprotected == []


async def test_the_audit_log_refuses_rewrites_even_from_the_platform(
    bus: PostgresEventBus,
) -> None:
    await _seed(bus)
    async with bus.pool.acquire() as connection:
        for statement in (
            f"UPDATE {bus.schema}.audit_log SET hash = 'x'",  # noqa: S608
            f"DELETE FROM {bus.schema}.audit_log",  # noqa: S608
            f"TRUNCATE {bus.schema}.audit_log",
        ):
            with pytest.raises(asyncpg.RaiseError, match="append-only"):
                await connection.execute(statement)


async def _superuser_rewrite(bus: PostgresEventBus, statement: str, *args: Any) -> None:
    async with bus.pool.acquire() as connection, connection.transaction():
        await connection.execute(f"ALTER TABLE {bus.schema}.audit_log DISABLE TRIGGER USER")
        await connection.execute(bus.sql(statement), *args)
        await connection.execute(f"ALTER TABLE {bus.schema}.audit_log ENABLE TRIGGER USER")


@pytest.mark.parametrize(
    ("statement", "problem"),
    [
        (
            "UPDATE {s}.audit_log SET entry = replace(entry, 'order', 'refund') WHERE seq = 2",
            "entry 2 was changed",
        ),
        ("DELETE FROM {s}.audit_log WHERE seq = 2", "entry 2 is missing"),
        ("DELETE FROM {s}.audit_log WHERE seq = 3", "the head expects 3 entries"),
    ],
)
async def test_tampering_with_the_chain_is_detected(
    bus: PostgresEventBus, statement: str, problem: str
) -> None:
    audit = PostgresAuditLog(bus)
    for _ in range(3):
        await audit.append(make_event("order.item.created", tenantid="acme"))
    assert (await audit.verify("acme")).ok
    await _superuser_rewrite(bus, statement)
    report = await audit.verify("acme")
    assert not report.ok
    assert report.problem == problem


async def test_a_chain_rebuilt_from_scratch_needs_its_head_rebuilt_too(
    bus: PostgresEventBus,
) -> None:
    """The documented limit: whoever rewrites every row and the head leaves no trace here."""
    audit = PostgresAuditLog(bus)
    rebuilt = InMemoryAuditLog()
    original = make_event("order.item.created", tenantid="acme")
    forged = make_event("refund.created", tenantid="acme")
    await audit.append(original)
    await rebuilt.append(forged)
    [entry] = await rebuilt.entries("acme")
    await _superuser_rewrite(
        bus,
        "UPDATE {s}.audit_log SET event_id = $1, entry = $2, hash = $3 WHERE seq = 1",
        entry.event_id,
        entry.entry,
        entry.hash,
    )
    assert not (await audit.verify("acme")).ok  # the head still has the old hash
    await _set_head(bus, entry.hash)
    assert (await audit.verify("acme")).ok


async def _set_head(bus: PostgresEventBus, value: Any) -> None:
    async with bus.pool.acquire() as connection:
        await connection.execute(f"UPDATE {bus.schema}.audit_heads SET hash = $1", value)  # noqa: S608


# --------------------------------------------------------------------------- approvals


async def _seed_approvals(bus: PostgresEventBus) -> PostgresApprovalStore:
    store = PostgresApprovalStore(bus)
    for tenant in ("acme", "globex"):
        await store.add(Approval.of(request(tenant=tenant)))
    return store


async def test_an_approval_query_with_no_filter_sees_one_tenant(bus: PostgresEventBus) -> None:
    await _seed_approvals(bus)
    for tenant in ("acme", "globex"):
        async with bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                f"SELECT tenantid FROM {bus.schema}.approvals"  # noqa: S608
            )
        assert {row["tenantid"] for row in rows} == {tenant}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE {s}.approvals SET status = 'granted'",
        "DELETE FROM {s}.approvals",
        "INSERT INTO {s}.approvals (tenantid) VALUES ('acme')",
    ],
)
async def test_a_tenant_connection_can_not_decide_an_approval(
    bus: PostgresEventBus, statement: str
) -> None:
    """Only the store does, in one transaction with the event the run listens for."""
    await _seed_approvals(bus)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with bus.tenant_connection("acme") as connection:
            await connection.execute(bus.sql(statement))


async def test_a_settle_names_its_tenant(bus: PostgresEventBus) -> None:
    """Writes run as the platform: without its own filter, this would close another's row."""
    store = await _seed_approvals(bus)
    [acme] = await store.find("acme", "pending")
    forged = Approval.of(acme.request.model_copy(update={"tenantid": "globex"}))
    result = await store.settle(
        forged,
        status="granted",
        approved=True,
        by=LEAD,
        comment=None,
        at=datetime.now(UTC),
        event=answer(acme),
    )
    assert result is None
    found = await store.get("acme", acme.id)
    assert found is not None
    assert found.status == "pending"

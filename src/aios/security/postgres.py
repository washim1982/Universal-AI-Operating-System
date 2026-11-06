"""The Security Kernel's tables, in the fabric's PostgreSQL database and schema.

The tables are created on first use. Every one that holds tenant data has row-level security
(FR-SEC-06), and what goes back to a tenant is read through a tenant connection. Decisions on the
hot path (roles, suspensions) filter by tenant themselves: their answer stays inside the platform,
and they run on every tool call.

The audit log can only grow: triggers refuse updates, deletes and truncation, which also stops
AIOS itself from rewriting history.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from aios.core.envelope import Event
from aios.core.identity import ApiKey, SecurityStore, Suspendable, Suspension
from aios.fabric.postgres import TENANT_ROLE, PostgresEventBus, tenant_only
from aios.security.audit import (
    GENESIS,
    AuditEntry,
    AuditLog,
    AuditReport,
    canonical,
    chained,
    check_chain,
)

_DDL = """
CREATE TABLE IF NOT EXISTS {s}.api_keys (
    id         text PRIMARY KEY,
    tenantid   text NOT NULL,
    name       text NOT NULL,
    principal  text NOT NULL,
    roles      text[] NOT NULL,
    digest     text NOT NULL,
    created_at timestamptz NOT NULL,
    created_by text NOT NULL,
    expires_at timestamptz,
    revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS api_keys_tenant ON {s}.api_keys (tenantid, created_at);
CREATE TABLE IF NOT EXISTS {s}.principals (
    tenantid  text NOT NULL,
    principal text NOT NULL,
    roles     text[] NOT NULL,
    PRIMARY KEY (tenantid, principal)
);
CREATE TABLE IF NOT EXISTS {s}.suspensions (
    tenantid text NOT NULL,
    kind     text NOT NULL,
    name     text NOT NULL,
    reason   text NOT NULL,
    by_whom  text NOT NULL,
    at       timestamptz NOT NULL,
    PRIMARY KEY (tenantid, kind, name)
);
CREATE TABLE IF NOT EXISTS {s}.audit_log (
    tenantid text NOT NULL,
    seq      bigint NOT NULL,
    event_id text NOT NULL,
    entry    text NOT NULL,
    previous text NOT NULL,
    hash     text NOT NULL,
    PRIMARY KEY (tenantid, seq),
    UNIQUE (tenantid, event_id)
);
CREATE TABLE IF NOT EXISTS {s}.audit_heads (
    tenantid text PRIMARY KEY,
    seq      bigint NOT NULL,
    hash     text NOT NULL
);
CREATE OR REPLACE FUNCTION {s}.audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'the audit log is append-only';
END $$;
CREATE OR REPLACE TRIGGER audit_no_rewrite BEFORE UPDATE OR DELETE ON {s}.audit_log
    FOR EACH ROW EXECUTE FUNCTION {s}.audit_append_only();
CREATE OR REPLACE TRIGGER audit_no_truncate BEFORE TRUNCATE ON {s}.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION {s}.audit_append_only();
"""


class _Tables:
    """Creates the tables on first use: the stores are built before the event loop is running."""

    def __init__(self, bus: PostgresEventBus) -> None:
        self.bus = bus
        self._ready = False
        self._lock = asyncio.Lock()

    async def ready(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            schema = self.bus.schema
            grants = ", ".join(f"{schema}.{t}" for t in ("audit_log", "audit_heads"))
            tenant_tables = ("api_keys", "principals", "suspensions", "audit_log", "audit_heads")
            statements = [
                self.bus.sql(_DDL),
                *(tenant_only(schema, table) for table in tenant_tables),
                f"GRANT SELECT ON {grants}, {schema}.suspensions, {schema}.principals "
                f"TO {TENANT_ROLE};",
                f"GRANT SELECT (id, tenantid, name, principal, roles, created_at, created_by, "
                f"expires_at, revoked_at) ON {schema}.api_keys TO {TENANT_ROLE};",
            ]
            await self.bus.set_up("security", "\n".join(statements))
            self._ready = True


_KEY_COLUMNS = (
    "id, tenantid, name, principal, roles, created_at, created_by, expires_at, revoked_at"
)


def _key(row: Any, digest: str = "") -> ApiKey:
    return ApiKey(
        id=row["id"],
        tenant=row["tenantid"],
        name=row["name"],
        principal=row["principal"],
        roles=frozenset(row["roles"]),
        digest=digest,
        created_at=row["created_at"],
        created_by=row["created_by"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


def _suspension(row: Any) -> Suspension:
    return Suspension(
        tenant=row["tenantid"],
        kind=Suspendable(row["kind"]),
        name=row["name"],
        reason=row["reason"],
        by=row["by_whom"],
        at=row["at"],
    )


class PostgresSecurityStore(SecurityStore):
    def __init__(self, bus: PostgresEventBus) -> None:
        self.bus = bus
        self._tables = _Tables(bus)

    async def _execute(self, query: str, *args: Any) -> str:
        await self._tables.ready()
        async with self.bus.pool.acquire() as connection:
            return await connection.execute(self.bus.sql(query), *args)

    async def add_key(self, key: ApiKey) -> None:
        await self._execute(
            "INSERT INTO {s}.api_keys (id, tenantid, name, principal, roles, digest, created_at, "
            "created_by, expires_at, revoked_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            key.id,
            key.tenant,
            key.name,
            key.principal,
            sorted(key.roles),
            key.digest,
            key.created_at,
            key.created_by,
            key.expires_at,
            key.revoked_at,
        )

    async def key(self, key_id: str) -> ApiKey | None:
        await self._tables.ready()
        async with self.bus.pool.acquire() as connection:
            row = await connection.fetchrow(
                self.bus.sql("SELECT * FROM {s}.api_keys WHERE id = $1"), key_id
            )
        return None if row is None else _key(row, row["digest"])

    async def keys(self, tenant: str) -> list[ApiKey]:
        await self._tables.ready()
        query = f"SELECT {_KEY_COLUMNS} FROM {{s}}.api_keys ORDER BY created_at, id"  # noqa: S608
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(self.bus.sql(query))
        return [_key(row) for row in rows]

    async def revoke_key(self, tenant: str, key_id: str, at: datetime) -> bool:
        status = await self._execute(
            "UPDATE {s}.api_keys SET revoked_at = $3 "
            "WHERE id = $1 AND tenantid = $2 AND revoked_at IS NULL",
            key_id,
            tenant,
            at,
        )
        return status == "UPDATE 1"

    async def set_roles(self, tenant: str, principal: str, roles: frozenset[str]) -> None:
        await self._execute(
            "INSERT INTO {s}.principals AS p (tenantid, principal, roles) VALUES ($1, $2, $3) "
            "ON CONFLICT (tenantid, principal) DO UPDATE SET roles = excluded.roles "
            "WHERE p.roles IS DISTINCT FROM excluded.roles",
            tenant,
            principal,
            sorted(roles),
        )

    async def roles(self, tenant: str, principal: str) -> frozenset[str]:
        await self._tables.ready()
        async with self.bus.pool.acquire() as connection:
            found = await connection.fetchval(
                self.bus.sql(
                    "SELECT roles FROM {s}.principals WHERE tenantid = $1 AND principal = $2"
                ),
                tenant,
                principal,
            )
        return frozenset(found or ())

    async def suspend(self, suspension: Suspension) -> None:
        await self._execute(
            "INSERT INTO {s}.suspensions (tenantid, kind, name, reason, by_whom, at) "
            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
            suspension.tenant,
            suspension.kind.value,
            suspension.name,
            suspension.reason,
            suspension.by,
            suspension.at,
        )

    async def lift(self, tenant: str, kind: Suspendable, name: str) -> bool:
        status = await self._execute(
            "DELETE FROM {s}.suspensions WHERE tenantid = $1 AND kind = $2 AND name = $3",
            tenant,
            kind.value,
            name,
        )
        return status == "DELETE 1"

    async def suspensions(self, tenant: str) -> list[Suspension]:
        await self._tables.ready()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql("SELECT * FROM {s}.suspensions ORDER BY at, kind, name")
            )
        return [_suspension(row) for row in rows]

    async def blocking(
        self, tenant: str, *, app: str | None = None, agent: str | None = None
    ) -> Suspension | None:
        await self._tables.ready()
        async with self.bus.pool.acquire() as connection:
            row = await connection.fetchrow(
                self.bus.sql(
                    "SELECT * FROM {s}.suspensions WHERE tenantid = $1 AND ("
                    "kind = 'tenant' OR (kind = 'app' AND name = $2) "
                    "OR (kind = 'agent' AND name = $3)) "
                    "ORDER BY CASE kind WHEN 'tenant' THEN 0 WHEN 'app' THEN 1 ELSE 2 END LIMIT 1"
                ),
                tenant,
                app,
                agent,
            )
        return None if row is None else _suspension(row)


def _entry(row: Any) -> AuditEntry:
    return AuditEntry(
        tenant=row["tenantid"],
        seq=row["seq"],
        event_id=row["event_id"],
        entry=row["entry"],
        previous=row["previous"],
        hash=row["hash"],
    )


class PostgresAuditLog(AuditLog):
    def __init__(self, bus: PostgresEventBus) -> None:
        self.bus = bus
        self._tables = _Tables(bus)

    async def append(self, event: Event) -> bool:
        await self._tables.ready()
        sql = self.bus.sql
        tenant = event.tenantid
        text = canonical(event)
        async with self.bus.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                sql("INSERT INTO {s}.audit_heads VALUES ($1, 0, $2) ON CONFLICT DO NOTHING"),
                tenant,
                GENESIS,
            )
            head = await connection.fetchrow(  # the row lock orders this tenant's appends
                sql("SELECT seq, hash FROM {s}.audit_heads WHERE tenantid = $1 FOR UPDATE"), tenant
            )
            recorded = await connection.fetchval(
                sql("SELECT 1 FROM {s}.audit_log WHERE tenantid = $1 AND event_id = $2"),
                tenant,
                event.id,
            )
            if recorded is not None:
                return False
            seq = head["seq"] + 1
            digest = chained(head["hash"], text)
            await connection.execute(
                sql("INSERT INTO {s}.audit_log VALUES ($1, $2, $3, $4, $5, $6)"),
                tenant,
                seq,
                event.id,
                text,
                head["hash"],
                digest,
            )
            await connection.execute(
                sql("UPDATE {s}.audit_heads SET seq = $2, hash = $3 WHERE tenantid = $1"),
                tenant,
                seq,
                digest,
            )
        return True

    async def entries(self, tenant: str, *, after: int = 0, limit: int = 500) -> list[AuditEntry]:
        await self._tables.ready()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql("SELECT * FROM {s}.audit_log WHERE seq > $1 ORDER BY seq LIMIT $2"),
                after,
                limit,
            )
        return [_entry(row) for row in rows]

    async def verify(self, tenant: str) -> AuditReport:
        await self._tables.ready()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(self.bus.sql("SELECT * FROM {s}.audit_log ORDER BY seq"))
            head = await connection.fetchrow(self.bus.sql("SELECT seq, hash FROM {s}.audit_heads"))
        return check_chain(
            [_entry(row) for row in rows],
            head_seq=head["seq"] if head is not None else None,
            head_hash=head["hash"] if head is not None else None,
        )

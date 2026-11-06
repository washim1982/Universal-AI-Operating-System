"""Approvals in the fabric's PostgreSQL database.

Closing an approval and publishing what happened are one transaction, through the fabric's outbox
(FR-EVT-07), so an approval can't be recorded as decided while its run never hears of it. Reads for
a tenant go through a tenant connection; writes use the connecting role, because a tenant
connection can only read, and name the tenant themselves.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from aios.core.envelope import Event
from aios.core.human import Approval, ApprovalStatus
from aios.fabric.postgres import TENANT_ROLE, PostgresEventBus, tenant_only
from aios.human.store import INBOX_LIMIT, OVERDUE_BATCH, ApprovalStore

_DDL = """
CREATE TABLE IF NOT EXISTS {s}.approvals (
    tenantid     text NOT NULL,
    id           text NOT NULL,
    runid        text,
    status       text NOT NULL,
    approved     boolean NOT NULL DEFAULT false,
    expires_at   timestamptz NOT NULL,
    request      jsonb NOT NULL,
    decided_by   text,
    comment      text,
    decided_at   timestamptz,
    PRIMARY KEY (tenantid, id)
);
CREATE INDEX IF NOT EXISTS approvals_inbox ON {s}.approvals (tenantid, status, id);
CREATE INDEX IF NOT EXISTS approvals_due ON {s}.approvals (expires_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS approvals_run ON {s}.approvals (tenantid, runid)
    WHERE status = 'pending';
"""


def _approval(row: Any) -> Approval:
    return Approval.of(
        Event.model_validate(row["request"]),
        status=row["status"],
        approved=row["approved"],
        by=row["decided_by"],
        comment=row["comment"],
        decided_at=row["decided_at"],
    )


class PostgresApprovalStore(ApprovalStore):
    def __init__(self, bus: PostgresEventBus) -> None:
        self.bus = bus
        self._ready = False
        self._lock = asyncio.Lock()

    async def _setup(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            schema = self.bus.schema
            statements = (
                self.bus.sql(_DDL),
                tenant_only(schema, "approvals"),
                f"GRANT SELECT ON {schema}.approvals TO {TENANT_ROLE};",
            )
            await self.bus.set_up("approvals", "\n".join(statements))
            self._ready = True

    async def add(self, approval: Approval) -> None:
        await self._setup()
        async with self.bus.pool.acquire() as connection:
            await connection.execute(
                self.bus.sql(
                    "INSERT INTO {s}.approvals (tenantid, id, runid, status, expires_at, request) "
                    "VALUES ($1, $2, $3, 'pending', $4, $5) ON CONFLICT DO NOTHING"
                ),
                approval.tenant,
                approval.id,
                approval.run,
                approval.expires_at,
                approval.request.model_dump(mode="json"),
            )

    async def get(self, tenant: str, approval_id: str) -> Approval | None:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            row = await connection.fetchrow(
                self.bus.sql("SELECT * FROM {s}.approvals WHERE id = $1"), approval_id
            )
        return None if row is None else _approval(row)

    async def find(
        self, tenant: str, status: ApprovalStatus, *, limit: int = INBOX_LIMIT
    ) -> list[Approval]:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql("SELECT * FROM {s}.approvals WHERE status = $1 ORDER BY id LIMIT $2"),
                status,
                limit,
            )
        return [_approval(row) for row in rows]

    async def in_run(self, tenant: str, run_id: str) -> list[Approval]:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql(
                    "SELECT * FROM {s}.approvals WHERE runid = $1 AND status = 'pending' "
                    "ORDER BY id"
                ),
                run_id,
            )
        return [_approval(row) for row in rows]

    async def overdue(self, now: datetime, *, limit: int = OVERDUE_BATCH) -> list[Approval]:
        await self._setup()
        async with self.bus.pool.acquire() as connection:
            rows = await connection.fetch(
                self.bus.sql(
                    "SELECT * FROM {s}.approvals WHERE status = 'pending' AND expires_at <= $1 "
                    "ORDER BY expires_at LIMIT $2"
                ),
                now,
                limit,
            )
        return [_approval(row) for row in rows]

    async def settle(
        self,
        approval: Approval,
        *,
        status: ApprovalStatus,
        approved: bool,
        by: str,
        comment: str | None,
        at: datetime,
        event: Event,
    ) -> Approval | None:
        await self._setup()
        async with self.bus.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                self.bus.sql(
                    "UPDATE {s}.approvals SET status = $3, approved = $4, decided_by = $5, "
                    "comment = $6, decided_at = $7 "
                    "WHERE tenantid = $1 AND id = $2 AND status = 'pending' RETURNING *"
                ),
                approval.tenant,
                approval.id,
                status,
                approved,
                by,
                comment,
                at,
            )
            if row is None:
                return None
            await self.bus.publish_in(connection, event)
        return _approval(row)

"""The spend ledger in the fabric's PostgreSQL database.

A charge and the event it pays for are one transaction, through the fabric's outbox (FR-EVT-07), and
every worker sees the same totals. Reads for a tenant go through a tenant connection; writes use the
connecting role, because a tenant connection can only read, and name the tenant themselves.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from aios.core.envelope import Event
from aios.fabric.postgres import TENANT_ROLE, PostgresEventBus, tenant_only
from aios.models.ledger import SpendLedger

_DDL = """
CREATE TABLE IF NOT EXISTS {s}.model_spend (
    tenantid  text NOT NULL,
    subject   text NOT NULL,
    usd       double precision NOT NULL DEFAULT 0,
    warned    boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenantid, subject)
);
"""


class PostgresSpendLedger(SpendLedger):
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
                tenant_only(schema, "model_spend"),
                f"GRANT SELECT ON {schema}.model_spend TO {TENANT_ROLE};",
            )
            await self.bus.set_up("spend", "\n".join(statements))
            self._ready = True

    async def spent(self, tenant: str, subjects: Sequence[str]) -> dict[str, float]:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql(
                    "SELECT subject, usd FROM {s}.model_spend "
                    "WHERE subject = ANY($1::text[]) AND usd > 0"
                ),
                list(subjects),
            )
        return {row["subject"]: row["usd"] for row in rows}

    async def charge(self, tenant: str, subjects: Sequence[str], usd: float, event: Event) -> None:
        await self._setup()
        async with self.bus.pool.acquire() as connection, connection.transaction():
            if usd > 0:
                await connection.execute(
                    self.bus.sql(
                        "INSERT INTO {s}.model_spend (tenantid, subject, usd) "
                        "SELECT $1, unnest($2::text[]), $3 "
                        "ON CONFLICT (tenantid, subject) "
                        "DO UPDATE SET usd = model_spend.usd + EXCLUDED.usd"
                    ),
                    tenant,
                    sorted(subjects),  # the same order everywhere, so that charges can't deadlock
                    usd,
                )
            await self.bus.publish_in(connection, event)

    async def warn_once(self, tenant: str, subject: str, event: Event) -> bool:
        await self._setup()
        async with self.bus.pool.acquire() as connection, connection.transaction():
            first = await connection.fetchval(
                self.bus.sql(
                    "INSERT INTO {s}.model_spend (tenantid, subject, warned) "
                    "VALUES ($1, $2, true) "
                    "ON CONFLICT (tenantid, subject) DO UPDATE SET warned = true "
                    "WHERE NOT model_spend.warned RETURNING true"
                ),
                tenant,
                subject,
            )
            if first is None:
                return False
            await self.bus.publish_in(connection, event)
        return True

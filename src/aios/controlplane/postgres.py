"""Installations in the fabric's PostgreSQL database.

A saved change and the event announcing it are one transaction, through the fabric's outbox
(FR-EVT-07), so a process that hears of a change always finds it. Reads for a tenant go through a
tenant connection; writes use the connecting role, because a tenant connection can only read.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from aios.controlplane.installations import InstallationStore
from aios.core.apps import Bundle, Installation
from aios.core.envelope import Event
from aios.fabric.postgres import TENANT_ROLE, PostgresEventBus, tenant_only

_DDL = """
CREATE TABLE IF NOT EXISTS {s}.app_versions (
    tenantid   text NOT NULL,
    app        text NOT NULL,
    version    text NOT NULL,
    added      bigserial,
    bundle     jsonb NOT NULL,
    PRIMARY KEY (tenantid, app, version)
);
CREATE TABLE IF NOT EXISTS {s}.app_installations (
    tenantid      text NOT NULL,
    app           text NOT NULL,
    installation  jsonb NOT NULL,
    PRIMARY KEY (tenantid, app)
);
CREATE TABLE IF NOT EXISTS {s}.app_ticks (
    tenantid    text NOT NULL,
    app         text NOT NULL,
    entrypoint  integer NOT NULL,
    tick        text NOT NULL,
    claimed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenantid, app, entrypoint, tick)
);
CREATE INDEX IF NOT EXISTS app_ticks_claimed ON {s}.app_ticks (claimed_at);
"""
_TABLES = ("app_versions", "app_installations", "app_ticks")


def _json(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else json.loads(value)


class PostgresInstallationStore(InstallationStore):
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
            statements = [
                self.bus.sql(_DDL),
                *(tenant_only(schema, table) for table in _TABLES),
                *(f"GRANT SELECT ON {schema}.{table} TO {TENANT_ROLE};" for table in _TABLES),
            ]
            await self.bus.set_up("installations", "\n".join(statements))
            self._ready = True

    async def add_version(self, tenant: str, bundle: Bundle) -> None:
        await self._setup()
        async with self.bus.pool.acquire() as connection:
            await connection.execute(
                self.bus.sql(
                    "INSERT INTO {s}.app_versions (tenantid, app, version, bundle) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING"
                ),
                tenant,
                bundle.name,
                bundle.version,
                bundle.model_dump(mode="json"),
            )

    async def version(self, tenant: str, app: str, version: str) -> Bundle | None:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            bundle = await connection.fetchval(
                self.bus.sql("SELECT bundle FROM {s}.app_versions WHERE app = $1 AND version = $2"),
                app,
                version,
            )
        return None if bundle is None else Bundle.model_validate(_json(bundle))

    async def versions(self, tenant: str, app: str) -> list[Bundle]:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql("SELECT bundle FROM {s}.app_versions WHERE app = $1 ORDER BY added"),
                app,
            )
        return [Bundle.model_validate(_json(row["bundle"])) for row in rows]

    async def get(self, tenant: str, app: str) -> Installation | None:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            found = await connection.fetchval(
                self.bus.sql("SELECT installation FROM {s}.app_installations WHERE app = $1"), app
            )
        return None if found is None else Installation.model_validate(_json(found))

    async def find(self, tenant: str) -> list[Installation]:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql("SELECT installation FROM {s}.app_installations ORDER BY app")
            )
        return [Installation.model_validate(_json(row["installation"])) for row in rows]

    async def everywhere(self) -> list[Installation]:
        await self._setup()
        async with self.bus.pool.acquire() as connection:
            rows = await connection.fetch(
                self.bus.sql("SELECT installation FROM {s}.app_installations")
            )
        return [Installation.model_validate(_json(row["installation"])) for row in rows]

    async def save(self, installation: Installation, *events: Event) -> None:
        await self._setup()
        async with self.bus.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                self.bus.sql(
                    "INSERT INTO {s}.app_installations (tenantid, app, installation) "
                    "VALUES ($1, $2, $3) ON CONFLICT (tenantid, app) "
                    "DO UPDATE SET installation = EXCLUDED.installation"
                ),
                installation.tenant,
                installation.app,
                installation.model_dump(mode="json"),
            )
            await self.bus.publish_in(connection, *events)

    async def claim(self, tenant: str, app: str, entrypoint: int, tick: str) -> bool:
        await self._setup()
        async with self.bus.pool.acquire() as connection:
            won = await connection.fetchval(
                self.bus.sql(
                    "INSERT INTO {s}.app_ticks (tenantid, app, entrypoint, tick) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING RETURNING true"
                ),
                tenant,
                app,
                entrypoint,
                tick,
            )
        return won is True

    async def forget_ticks(self, before: datetime) -> None:
        await self._setup()
        async with self.bus.pool.acquire() as connection:
            await connection.execute(
                self.bus.sql("DELETE FROM {s}.app_ticks WHERE claimed_at < $1"), before
            )

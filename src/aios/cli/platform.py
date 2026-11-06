"""``aios platform``: what only the operator of an installation can do, with direct database access.

The first key of a tenant can't come through the API, and nor can ``platform-admin``. Lifting a
tenant's suspension is here too, because a suspended tenant can't sign in to ask.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta

from aios.controlplane import Runtime
from aios.core.identity import Identity, Suspendable
from aios.fabric.postgres import PostgresEventBus
from aios.security.identity import describe

OPERATOR = "system:platform"


@asynccontextmanager
async def _installation(database_url: str) -> AsyncGenerator[Runtime]:
    async with PostgresEventBus(database_url) as bus:
        yield Runtime(bus)


async def create_key(
    database_url: str, tenant: str, name: str, roles: Sequence[str], *, days: int | None
) -> tuple[dict[str, object], str]:
    """A new API key's public fields, and its value."""
    async with _installation(database_url) as runtime:
        key, value = await runtime.create_key(
            Identity(OPERATOR, tenant),
            name,
            roles,
            expires_in=timedelta(days=days) if days else None,
        )
    return describe(key), value


async def suspend_tenant(database_url: str, tenant: str, reason: str) -> None:
    async with _installation(database_url) as runtime:
        await runtime.suspend(Identity(OPERATOR, tenant), Suspendable.TENANT, tenant, reason=reason)


async def lift_tenant(database_url: str, tenant: str) -> bool:
    async with _installation(database_url) as runtime:
        return await runtime.lift_suspension(Identity(OPERATOR, tenant), Suspendable.TENANT, tenant)

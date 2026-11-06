"""The in-memory :class:`SecurityStore`, for ``aios dev`` and tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from aios.core.identity import ApiKey, SecurityStore, Suspendable, Suspension
from aios.fabric.port import EventBus
from aios.fabric.postgres import PostgresEventBus
from aios.security.audit import AuditLog, InMemoryAuditLog


def stores_for(bus: EventBus) -> tuple[SecurityStore, AuditLog]:
    """The stores for a fabric: its own database, or memory for the in-memory fabric."""
    if isinstance(bus, PostgresEventBus):
        from aios.security.postgres import (  # noqa: PLC0415 - only with a database
            PostgresAuditLog,
            PostgresSecurityStore,
        )

        return PostgresSecurityStore(bus), PostgresAuditLog(bus)
    return InMemorySecurityStore(), InMemoryAuditLog()


class InMemorySecurityStore(SecurityStore):
    def __init__(self) -> None:
        self._keys: dict[str, ApiKey] = {}
        self._roles: dict[tuple[str, str], frozenset[str]] = {}
        self._suspensions: dict[tuple[str, Suspendable, str], Suspension] = {}

    async def add_key(self, key: ApiKey) -> None:
        self._keys[key.id] = key

    async def key(self, key_id: str) -> ApiKey | None:
        return self._keys.get(key_id)

    async def keys(self, tenant: str) -> list[ApiKey]:
        return sorted(
            (key for key in self._keys.values() if key.tenant == tenant),
            key=lambda key: (key.created_at, key.id),
        )

    async def revoke_key(self, tenant: str, key_id: str, at: datetime) -> bool:
        key = self._keys.get(key_id)
        if key is None or key.tenant != tenant or key.revoked_at is not None:
            return False
        self._keys[key_id] = replace(key, revoked_at=at)
        return True

    async def set_roles(self, tenant: str, principal: str, roles: frozenset[str]) -> None:
        self._roles[(tenant, principal)] = roles

    async def roles(self, tenant: str, principal: str) -> frozenset[str]:
        return self._roles.get((tenant, principal), frozenset())

    async def suspend(self, suspension: Suspension) -> None:
        key = (suspension.tenant, suspension.kind, suspension.name)
        self._suspensions.setdefault(key, suspension)

    async def lift(self, tenant: str, kind: Suspendable, name: str) -> bool:
        return self._suspensions.pop((tenant, kind, name), None) is not None

    async def suspensions(self, tenant: str) -> list[Suspension]:
        return sorted(
            (s for s in self._suspensions.values() if s.tenant == tenant),
            key=lambda s: (s.at, s.kind, s.name),
        )

    async def blocking(
        self, tenant: str, *, app: str | None = None, agent: str | None = None
    ) -> Suspension | None:
        for kind, name in (
            (Suspendable.TENANT, tenant),
            (Suspendable.APP, app),
            (Suspendable.AGENT, agent),
        ):
            found = self._suspensions.get((tenant, kind, name or ""))
            if found is not None:
                return found
        return None

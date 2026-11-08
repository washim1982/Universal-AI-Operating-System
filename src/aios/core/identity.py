"""Who is calling (ADR-011; FR-SEC-01, 02, 13).

The tenant of a request comes from an :class:`Identity`, which comes from a credential, never from
the request itself. :class:`SecurityStore` keeps what the Security Kernel must remember across
processes: API keys, the roles of each principal and active suspensions. PostgreSQL holds it in
production (:mod:`aios.security.postgres`); the adapters share one conformance suite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from aios.core.policy import Actor


@dataclass(frozen=True, slots=True)
class Identity:
    """An authenticated caller."""

    principal: str
    """``user:<sub>@<tenant>``, ``service:<key name>@<tenant>`` or, locally, ``user:anonymous``."""
    tenant: str
    roles: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()
    """Groups the identity provider says this person is in; the tenant's directory adds more."""

    @property
    def actor(self) -> Actor:
        return Actor(self.principal, self.roles)


class Authenticator(ABC):
    @abstractmethod
    async def authenticate(self, credential: str) -> Identity:
        """The identity a bearer credential proves, or :class:`AuthenticationError`."""


class Suspendable(StrEnum):
    """What the kill switch can stop (FR-SEC-13)."""

    AGENT = "agent"
    APP = "app"
    TENANT = "tenant"


@dataclass(frozen=True, slots=True)
class Suspension:
    tenant: str
    kind: Suspendable
    name: str
    reason: str
    by: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ApiKey:
    """An API key's record. The secret itself is never stored, only ``digest``."""

    id: str
    tenant: str
    name: str
    principal: str
    roles: frozenset[str]
    digest: str = field(repr=False)
    created_at: datetime
    created_by: str
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class SecurityStore(ABC):
    # ------------------------------------------------------------------ API keys

    @abstractmethod
    async def add_key(self, key: ApiKey) -> None: ...

    @abstractmethod
    async def key(self, key_id: str) -> ApiKey | None:
        """Any tenant's key: the lookup that happens before the caller's tenant is known."""

    @abstractmethod
    async def keys(self, tenant: str) -> list[ApiKey]: ...

    @abstractmethod
    async def revoke_key(self, tenant: str, key_id: str, at: datetime) -> bool:
        """Revoke a tenant's key; ``False`` if it has no such active key."""

    # ------------------------------------------------------------------ roles

    @abstractmethod
    async def set_roles(self, tenant: str, principal: str, roles: frozenset[str]) -> None: ...

    @abstractmethod
    async def roles(self, tenant: str, principal: str) -> frozenset[str]: ...

    @abstractmethod
    async def holders(self, tenant: str, role: str) -> list[str]:
        """The tenant's principals that have ``role``, as last signed in or given a key."""

    # ------------------------------------------------------------------ the kill switch

    @abstractmethod
    async def suspend(self, suspension: Suspension) -> None:
        """Stop ``kind``/``name``. Suspending what is already suspended keeps the first record."""

    @abstractmethod
    async def lift(self, tenant: str, kind: Suspendable, name: str) -> bool:
        """End a suspension; ``False`` if there was none."""

    @abstractmethod
    async def suspensions(self, tenant: str) -> list[Suspension]: ...

    @abstractmethod
    async def blocking(
        self, tenant: str, *, app: str | None = None, agent: str | None = None
    ) -> Suspension | None:
        """A suspension that stops this tenant, or this app or agent in it."""

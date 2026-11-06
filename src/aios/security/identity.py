"""Authentication (ADR-011; FR-SEC-02): API keys for services and OIDC bearer tokens for people.

* **API keys** look like ``aios_<id>_<secret>``. The secret has 256 bits of entropy, so a plain
  SHA-256 digest is enough, and it is the only thing stored.
* **OIDC tokens** are validated against the issuer's published keys, with RS256 or ES256 only, and
  ``iss``, ``aud``, ``exp`` and ``sub`` required. The tenant and the roles come from configurable
  claims.

Either way, signing in copies the caller's roles to the directory. A worker process finds them
there when it decides a tool call for a run this caller started.

The tenant of a request always comes from here, never from the request (ADR-011).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

import httpx2
import jwt

from aios.core.envelope import PRINCIPAL_PATTERN
from aios.core.errors import AuthenticationError
from aios.core.identity import ApiKey, Authenticator, Identity, SecurityStore
from aios.core.ids import new_ulid
from aios.core.topics import is_dns_label

logger = logging.getLogger("aios.security")

KEY_PREFIX = "aios"
OIDC_ALGORITHMS = ("RS256", "ES256")
JWKS_TTL_S = 3600.0
JWKS_MIN_REFRESH_S = 30.0
"""An unknown key id forces a refresh, at most this often, so bad tokens can't hammer the issuer."""
_PRINCIPAL = re.compile(PRINCIPAL_PATTERN)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_key(
    tenant: str,
    name: str,
    roles: Collection[str],
    *,
    created_by: str,
    expires_in: timedelta | None = None,
) -> tuple[ApiKey, str]:
    """A new API key's record and its value, which is shown once and stored nowhere."""
    if not is_dns_label(tenant):
        raise ValueError(f"invalid tenant id: {tenant!r}")
    if not is_dns_label(name):
        raise ValueError(f"a key name must be a DNS label: {name!r}")
    key_id = new_ulid()
    secret = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    key = ApiKey(
        id=key_id,
        tenant=tenant,
        name=name,
        principal=f"service:{name}@{tenant}",
        roles=frozenset(roles),
        digest=_digest(secret),
        created_at=now,
        created_by=created_by,
        expires_at=now + expires_in if expires_in is not None else None,
    )
    return key, f"{KEY_PREFIX}_{key_id}_{secret}"


class ApiKeys(Authenticator):
    def __init__(self, store: SecurityStore) -> None:
        self.store = store

    async def authenticate(self, credential: str) -> Identity:
        parts = credential.split("_", 2)
        if len(parts) != 3 or parts[0] != KEY_PREFIX:
            raise AuthenticationError("not an AIOS API key")
        key = await self.store.key(parts[1])
        if key is None or not hmac.compare_digest(key.digest, _digest(parts[2])):
            raise AuthenticationError("unknown API key")
        if key.revoked_at is not None:
            raise AuthenticationError("this API key was revoked")
        if key.expires_at is not None and key.expires_at <= datetime.now(UTC):
            raise AuthenticationError("this API key expired")
        await self.store.set_roles(key.tenant, key.principal, key.roles)
        return Identity(key.principal, key.tenant, key.roles)


@dataclass(frozen=True, slots=True)
class OidcSettings:
    issuer: str
    audience: str
    tenant_claim: str = "aios_tenant"
    roles_claim: str = "roles"
    """A claim, or a dotted path such as ``realm_access.roles`` (Keycloak's realm roles)."""


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Oidc(Authenticator):
    def __init__(
        self,
        settings: OidcSettings,
        store: SecurityStore,
        *,
        http: httpx2.AsyncClient | None = None,
    ) -> None:
        url = urlsplit(settings.issuer)
        if url.scheme != "https" and not is_loopback(url.hostname or ""):
            raise ValueError(f"an OIDC issuer must use https: {settings.issuer}")
        self.settings = settings
        self.store = store
        self.http = http or httpx2.AsyncClient(timeout=10.0)
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = -JWKS_TTL_S
        self._lock = asyncio.Lock()

    async def authenticate(self, credential: str) -> Identity:
        claims = await self._verified(credential)
        tenant = self._claim(claims, self.settings.tenant_claim)
        if not isinstance(tenant, str) or not is_dns_label(tenant):
            raise AuthenticationError(f"the token has no tenant in {self.settings.tenant_claim}")
        principal = f"user:{claims['sub']}@{tenant}"
        if _PRINCIPAL.fullmatch(principal) is None:
            raise AuthenticationError("the token's subject can't name a principal")
        roles = self._claim(claims, self.settings.roles_claim)
        granted = (
            frozenset(role for role in cast(list[Any], roles) if isinstance(role, str))
            if isinstance(roles, list)
            else frozenset[str]()
        )
        await self.store.set_roles(tenant, principal, granted)
        return Identity(principal, tenant, granted)

    @staticmethod
    def _claim(claims: Mapping[str, Any], path: str) -> Any:
        value: Any = claims
        for name in path.split("."):
            if not isinstance(value, dict):
                return None
            value = cast(dict[str, Any], value).get(name)
        return value

    async def _verified(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthenticationError("not a valid token") from exc
        key = await self._key(str(header.get("kid")))
        try:
            return jwt.decode(
                token,
                key,
                algorithms=list(OIDC_ALGORITHMS),
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            logger.info("rejected a token: %s", exc)
            raise AuthenticationError("not a valid token") from exc

    async def _key(self, key_id: str) -> jwt.PyJWK:
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            if age > JWKS_TTL_S or (key_id not in self._keys and age > JWKS_MIN_REFRESH_S):
                await self._refresh()
            key = self._keys.get(key_id)
        if key is None:
            raise AuthenticationError("the token was signed with an unknown key")
        return key

    async def _refresh(self) -> None:
        try:
            base = self.settings.issuer.rstrip("/")
            found = await self.http.get(f"{base}/.well-known/openid-configuration")
            found.raise_for_status()
            keys = await self.http.get(found.json()["jwks_uri"])
            keys.raise_for_status()
            published = jwt.PyJWKSet.from_dict(keys.json())
        except (httpx2.HTTPError, KeyError, ValueError, jwt.PyJWTError) as exc:
            logger.warning("could not fetch the issuer's signing keys: %s", exc)
            if not self._keys:
                raise AuthenticationError("the identity provider can't be reached") from exc
            return  # keep the keys we have; rotation can wait for the next attempt
        self._keys = {key.key_id: key for key in published.keys if key.key_id is not None}
        self._fetched_at = time.monotonic()


class Credentials(Authenticator):
    """API keys and, when an issuer is configured, OIDC tokens."""

    def __init__(self, keys: ApiKeys, oidc: Oidc | None = None) -> None:
        self.keys = keys
        self.oidc = oidc

    async def authenticate(self, credential: str) -> Identity:
        if credential.startswith(f"{KEY_PREFIX}_") or self.oidc is None:
            return await self.keys.authenticate(credential)
        return await self.oidc.authenticate(credential)


def describe(key: ApiKey) -> dict[str, Any]:
    """A key's public fields. Never the digest."""
    return {
        "id": key.id,
        "name": key.name,
        "principal": key.principal,
        "roles": sorted(key.roles),
        "created_at": key.created_at.isoformat(),
        "created_by": key.created_by,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
    }

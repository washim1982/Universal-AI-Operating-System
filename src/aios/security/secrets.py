"""Service credentials behind the ``SecretStore`` port (FR-TOOL-06, FR-SEC-07).

* :class:`EnvSecrets`: ``AIOS_SECRET_<NAME>`` for every tenant on this machine, so it suits one
  tenant on a developer's machine.
* :class:`EncryptedFileSecrets`: a local file encrypted with AES-256-GCM. Each value's tenant, App
  and name are authenticated with it, so a value copied to another tenant's slot won't decrypt.
* :class:`VaultSecrets`: HashiCorp Vault or OpenBao, KV version 2, one path per tenant and App.

Secrets are fetched at execution time and kept nowhere (FR-SEC-07).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, cast

import httpx2
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aios.core.errors import AIOSError
from aios.core.ports import SecretStore

logger = logging.getLogger("aios.security")

ENV_PREFIX = "AIOS_SECRET_"
KEY_BYTES = 32
_NONCE_BYTES = 12


class SecretStoreError(AIOSError):
    """The secret store can't be read or doesn't accept its key (FR-SEC-07)."""

    code = "secrets.unavailable"


class EnvSecrets(SecretStore):
    async def get(self, tenant: str, app: str, name: str) -> str | None:
        return os.environ.get(f"{ENV_PREFIX}{name}") or None


def new_store_key() -> str:
    """A key for :class:`EncryptedFileSecrets`, for ``AIOS_SECRETS_KEY``."""
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode()


class EncryptedFileSecrets(SecretStore):
    """Holds ``{"<tenant>/<app>/<name>": "<nonce + ciphertext, base64>"}`` in one JSON file."""

    def __init__(self, path: str | Path, key: str) -> None:
        self.path = Path(path)
        try:
            raw = base64.urlsafe_b64decode(key.encode())
        except ValueError as exc:
            raise SecretStoreError("AIOS_SECRETS_KEY is not valid base64") from exc
        if len(raw) != KEY_BYTES:
            raise SecretStoreError(f"AIOS_SECRETS_KEY must decode to {KEY_BYTES} bytes")
        self._cipher = AESGCM(raw)

    @staticmethod
    def _slot(tenant: str, app: str, name: str) -> str:
        return f"{tenant}/{app}/{name}"

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return cast(dict[str, str], json.loads(self.path.read_text("utf-8")))

    def put(self, tenant: str, app: str, name: str, value: str) -> None:
        slot = self._slot(tenant, app, name)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        sealed = self._cipher.encrypt(nonce, value.encode(), slot.encode())
        stored = {**self._read(), slot: base64.b64encode(nonce + sealed).decode()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.touch(mode=0o600)
        temporary.write_text(json.dumps(stored, indent=2, sort_keys=True), "utf-8")
        temporary.replace(self.path)

    async def get(self, tenant: str, app: str, name: str) -> str | None:
        slot = self._slot(tenant, app, name)
        stored = self._read().get(slot)
        if stored is None:
            return None
        blob = base64.b64decode(stored)
        try:
            opened = self._cipher.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], slot.encode())
        except InvalidTag as exc:
            raise SecretStoreError(f"the secret {name} can't be decrypted") from exc
        return opened.decode()


class VaultSecrets(SecretStore):
    """``<mount>/data/aios/<tenant>/<app>/<name>``, with the value in the field ``value``."""

    def __init__(
        self,
        address: str,
        token: str,
        *,
        mount: str = "secret",
        http: httpx2.AsyncClient | None = None,
    ) -> None:
        self.address = address.rstrip("/")
        self.mount = mount
        self.http = http or httpx2.AsyncClient(timeout=10.0)
        self._headers = {"X-Vault-Token": token}

    async def get(self, tenant: str, app: str, name: str) -> str | None:
        url = f"{self.address}/v1/{self.mount}/data/aios/{tenant}/{app}/{name}"
        try:
            response = await self.http.get(url, headers=self._headers)
        except httpx2.HTTPError as exc:
            raise SecretStoreError(f"Vault can't be reached: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SecretStoreError(f"Vault answered {response.status_code}")
        data: Any = response.json().get("data", {}).get("data") or {}
        fields = cast(dict[str, Any], data) if isinstance(data, dict) else {}
        value = fields.get("value")
        return value if isinstance(value, str) and value else None


def secrets_from_environment() -> SecretStore:
    """Vault if ``VAULT_ADDR`` is set, else the encrypted file, else the environment."""
    address, token = os.environ.get("VAULT_ADDR"), os.environ.get("VAULT_TOKEN")
    if address:
        if not token:
            raise SecretStoreError("VAULT_ADDR needs VAULT_TOKEN")
        return VaultSecrets(address, token, mount=os.environ.get("AIOS_VAULT_MOUNT", "secret"))
    path, key = os.environ.get("AIOS_SECRETS_FILE"), os.environ.get("AIOS_SECRETS_KEY")
    if path:
        if not key:
            raise SecretStoreError("AIOS_SECRETS_FILE needs AIOS_SECRETS_KEY")
        return EncryptedFileSecrets(path, key)
    return EnvSecrets()

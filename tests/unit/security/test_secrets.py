"""The secret stores (FR-SEC-07)."""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.security.secrets import (
    EncryptedFileSecrets,
    EnvSecrets,
    SecretStoreError,
    VaultSecrets,
    new_store_key,
    secrets_from_environment,
)

VALUE = "sk-live-super-secret"


@pytest.fixture
def store(tmp_path: Path) -> EncryptedFileSecrets:
    return EncryptedFileSecrets(tmp_path / "secrets.json", new_store_key())


async def test_a_stored_secret_comes_back(store: EncryptedFileSecrets) -> None:
    store.put("acme", "desk", "STRIPE_KEY", VALUE)
    assert await store.get("acme", "desk", "STRIPE_KEY") == VALUE


async def test_a_missing_secret_is_none(store: EncryptedFileSecrets) -> None:
    assert await store.get("acme", "desk", "STRIPE_KEY") is None


def test_the_file_never_holds_the_value(store: EncryptedFileSecrets) -> None:
    store.put("acme", "desk", "STRIPE_KEY", VALUE)
    assert VALUE not in store.path.read_text("utf-8")


async def test_secrets_are_kept_per_tenant_and_app(store: EncryptedFileSecrets) -> None:
    store.put("acme", "desk", "STRIPE_KEY", "a")
    store.put("globex", "desk", "STRIPE_KEY", "b")
    store.put("acme", "other", "STRIPE_KEY", "c")
    assert await store.get("acme", "desk", "STRIPE_KEY") == "a"
    assert await store.get("globex", "desk", "STRIPE_KEY") == "b"
    assert await store.get("acme", "other", "STRIPE_KEY") == "c"


async def test_a_value_moved_to_another_tenants_slot_will_not_decrypt(
    store: EncryptedFileSecrets,
) -> None:
    """Someone with write access to the file can't hand acme's secret to globex."""
    store.put("acme", "desk", "STRIPE_KEY", VALUE)
    stored: dict[str, str] = json.loads(store.path.read_text("utf-8"))
    stored["globex/desk/STRIPE_KEY"] = stored["acme/desk/STRIPE_KEY"]
    store.path.write_text(json.dumps(stored), "utf-8")
    with pytest.raises(SecretStoreError):
        await store.get("globex", "desk", "STRIPE_KEY")


async def test_the_wrong_key_can_not_read_the_file(store: EncryptedFileSecrets) -> None:
    store.put("acme", "desk", "STRIPE_KEY", VALUE)
    other = EncryptedFileSecrets(store.path, new_store_key())
    with pytest.raises(SecretStoreError) as raised:
        await other.get("acme", "desk", "STRIPE_KEY")
    assert VALUE not in str(raised.value)


def test_each_write_uses_a_fresh_nonce(store: EncryptedFileSecrets) -> None:
    store.put("acme", "desk", "A", VALUE)
    first = json.loads(store.path.read_text("utf-8"))["acme/desk/A"]
    store.put("acme", "desk", "A", VALUE)
    assert json.loads(store.path.read_text("utf-8"))["acme/desk/A"] != first


@pytest.mark.parametrize("key", ["short", "not base64 !!!", ""])
def test_a_bad_store_key_is_refused(tmp_path: Path, key: str) -> None:
    with pytest.raises(SecretStoreError):
        EncryptedFileSecrets(tmp_path / "s.json", key)


class Vault:
    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []
        self.status = 200

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        if self.status != 200:
            return httpx2.Response(self.status, json={"errors": ["no"]})
        if request.url.path.endswith("/MISSING"):
            return httpx2.Response(404, json={"errors": []})
        payload: dict[str, Any] = {"data": {"data": {"value": VALUE}, "metadata": {"version": 1}}}
        return httpx2.Response(200, json=payload)

    def client(self) -> VaultSecrets:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))
        return VaultSecrets("https://vault.test/", "the-token", http=http)


async def test_vault_reads_a_path_per_tenant_and_app() -> None:
    vault = Vault()
    assert await vault.client().get("acme", "desk", "STRIPE_KEY") == VALUE
    [request] = vault.seen
    assert request.url.path == "/v1/secret/data/aios/acme/desk/STRIPE_KEY"
    assert request.headers["X-Vault-Token"] == "the-token"


async def test_vault_answers_none_for_a_missing_secret() -> None:
    assert await Vault().client().get("acme", "desk", "MISSING") is None


@pytest.mark.parametrize("status", [403, 500, 503])
async def test_vault_errors_are_errors_not_empty_secrets(status: int) -> None:
    vault = Vault()
    vault.status = status
    with pytest.raises(SecretStoreError, match=str(status)):
        await vault.client().get("acme", "desk", "STRIPE_KEY")


async def test_an_unreachable_vault_says_so_without_the_token() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
    with pytest.raises(SecretStoreError, match="can't be reached") as raised:
        await VaultSecrets("https://v.test", "the-token", http=http).get("a", "b", "C")
    assert "the-token" not in str(raised.value)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("VAULT_ADDR", "VAULT_TOKEN", "AIOS_SECRETS_FILE", "AIOS_SECRETS_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_the_environment_chooses_the_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear(monkeypatch)
    assert isinstance(secrets_from_environment(), EnvSecrets)
    monkeypatch.setenv("AIOS_SECRETS_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("AIOS_SECRETS_KEY", new_store_key())
    assert isinstance(secrets_from_environment(), EncryptedFileSecrets)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_TOKEN", "t")
    assert isinstance(secrets_from_environment(), VaultSecrets)


@pytest.mark.parametrize(
    ("name", "needs"), [("VAULT_ADDR", "VAULT_TOKEN"), ("AIOS_SECRETS_FILE", "AIOS_SECRETS_KEY")]
)
def test_half_a_configuration_is_an_error(
    monkeypatch: pytest.MonkeyPatch, name: str, needs: str
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(name, "x")
    with pytest.raises(SecretStoreError, match=needs):
        secrets_from_environment()

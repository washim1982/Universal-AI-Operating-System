"""API keys and OIDC bearer tokens (ADR-011; FR-SEC-02). OIDC tokens here are really signed."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from aios.core.errors import AuthenticationError
from aios.security.identity import (
    ApiKeys,
    Credentials,
    Oidc,
    OidcSettings,
    is_loopback,
    new_key,
)
from aios.security.store import InMemorySecurityStore

ISSUER = "https://id.example.test/realms/aios"
AUDIENCE = "aios-api"
KEY_ID = "signing-key"


# --------------------------------------------------------------------------- API keys


@pytest.fixture
def store() -> InMemorySecurityStore:
    return InMemorySecurityStore()


async def test_a_key_proves_its_tenant_principal_and_roles(store: InMemorySecurityStore) -> None:
    key, value = new_key("acme", "ci", ["operator"], created_by="system:test")
    await store.add_key(key)
    identity = await ApiKeys(store).authenticate(value)
    assert identity.tenant == "acme"
    assert identity.principal == "service:ci@acme"
    assert identity.roles == {"operator"}


async def test_only_a_digest_of_the_secret_is_stored() -> None:
    key, value = new_key("acme", "ci", ["member"], created_by="system:test")
    secret = value.split("_", 2)[2]
    assert secret not in repr(key)
    assert secret != key.digest
    assert key.digest not in repr(key)


@pytest.mark.parametrize(
    "wrong",
    [
        "",
        "aios",
        "aios_",
        "aios__",
        "aios_NOPE_secret",
        "sk-ant-not-ours",
    ],
)
async def test_malformed_keys_are_refused(store: InMemorySecurityStore, wrong: str) -> None:
    with pytest.raises(AuthenticationError):
        await ApiKeys(store).authenticate(wrong)


async def test_a_right_id_with_a_wrong_secret_is_refused(store: InMemorySecurityStore) -> None:
    key, value = new_key("acme", "ci", ["member"], created_by="system:test")
    await store.add_key(key)
    with pytest.raises(AuthenticationError):
        await ApiKeys(store).authenticate(value[:-1] + ("A" if value[-1] != "A" else "B"))


async def test_a_revoked_key_stops_working_at_once(store: InMemorySecurityStore) -> None:
    key, value = new_key("acme", "ci", ["member"], created_by="system:test")
    await store.add_key(key)
    assert await store.revoke_key("acme", key.id, datetime.now(UTC))
    with pytest.raises(AuthenticationError, match="revoked"):
        await ApiKeys(store).authenticate(value)
    assert not await store.revoke_key("acme", key.id, datetime.now(UTC))


async def test_an_expired_key_stops_working(store: InMemorySecurityStore) -> None:
    key, value = new_key(
        "acme", "ci", ["member"], created_by="system:test", expires_in=timedelta(days=-1)
    )
    await store.add_key(key)
    with pytest.raises(AuthenticationError, match="expired"):
        await ApiKeys(store).authenticate(value)


@pytest.mark.parametrize(
    ("tenant", "name"), [("*", "ci"), ("ACME", "ci"), ("acme", "a b"), ("acme", "x@y")]
)
def test_a_key_needs_a_valid_tenant_and_name(tenant: str, name: str) -> None:
    with pytest.raises(ValueError, match=r"invalid tenant|DNS label"):
        new_key(tenant, name, ["member"], created_by="system:test")


# --------------------------------------------------------------------------- OIDC


rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _claims(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "aios_tenant": "acme",
        "roles": ["member", "support-lead"],
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(key: Any = rsa_key, algorithm: str = "RS256", kid: str = KEY_ID, **claims: Any) -> str:
    return jwt.encode(_claims(**claims), key, algorithm=algorithm, headers={"kid": kid})


def _jwk(key: Any, kid: str = KEY_ID) -> dict[str, Any]:
    public = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return {**public, "kid": kid, "use": "sig", "alg": "RS256"}


class Issuer:
    """Discovery and JWKS endpoints, counted."""

    def __init__(self) -> None:
        self.keys = [_jwk(rsa_key)]
        self.fetches = 0
        self.down = False

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.down:
            raise httpx2.ConnectError("issuer is down")
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx2.Response(200, json={"jwks_uri": f"{ISSUER}/certs"})
        self.fetches += 1
        return httpx2.Response(200, json={"keys": self.keys})


@pytest.fixture
def issuer() -> Issuer:
    return Issuer()


@pytest.fixture
async def oidc(issuer: Issuer, store: InMemorySecurityStore) -> AsyncIterator[Oidc]:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(issuer.handle))
    yield Oidc(OidcSettings(ISSUER, AUDIENCE), store, http=http)
    await http.aclose()


async def test_a_valid_token_names_the_user_tenant_and_roles(
    oidc: Oidc, store: InMemorySecurityStore
) -> None:
    identity = await oidc.authenticate(_token())
    assert identity.principal == "user:alice@acme"
    assert identity.tenant == "acme"
    assert identity.roles == {"member", "support-lead"}
    assert await store.roles("acme", "user:alice@acme") == {"member", "support-lead"}


async def test_sign_in_updates_the_roles_the_directory_holds(
    oidc: Oidc, store: InMemorySecurityStore
) -> None:
    await oidc.authenticate(_token())
    await oidc.authenticate(_token(roles=["member"]))
    assert await store.roles("acme", "user:alice@acme") == {"member"}


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.example.test"},
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"exp": None},
        {"sub": None},
        {"aud": None},
        {"iat": None},
        {"aios_tenant": None},
        {"aios_tenant": "*"},
        {"aios_tenant": "ACME"},
        {"aios_tenant": 7},
        {"sub": "not a valid name"},
    ],
    ids=str,
)
async def test_tokens_with_bad_claims_are_refused(oidc: Oidc, claims: dict[str, Any]) -> None:
    with pytest.raises(AuthenticationError):
        await oidc.authenticate(_token(**claims))


async def test_a_token_signed_by_another_key_is_refused(oidc: Oidc) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthenticationError):
        await oidc.authenticate(_token(key=other))


async def test_an_unsigned_token_is_refused(oidc: Oidc) -> None:
    await oidc.authenticate(_token())  # the key is known, so only the algorithm can refuse this
    forged = jwt.encode(_claims(), key="", algorithm="none", headers={"kid": KEY_ID})
    with pytest.raises(AuthenticationError):
        await oidc.authenticate(forged)


async def test_the_public_key_can_not_be_used_as_an_hmac_secret(oidc: Oidc) -> None:
    """The classic confusion: sign with HS256 using the published RSA key as the secret."""
    await oidc.authenticate(_token())
    secret = json.dumps(_jwk(rsa_key)).encode() + b"-padding-to-32-bytes-or-more"
    forged = jwt.encode(_claims(), secret, algorithm="HS256", headers={"kid": KEY_ID})
    with pytest.raises(AuthenticationError):
        await oidc.authenticate(forged)


async def test_an_algorithm_outside_the_allow_list_is_refused(oidc: Oidc, issuer: Issuer) -> None:
    await oidc.authenticate(_token())
    with pytest.raises(AuthenticationError):
        await oidc.authenticate(_token(algorithm="RS512"))
    assert issuer.fetches == 1


async def test_keys_are_cached_and_a_new_key_id_refreshes_them(oidc: Oidc, issuer: Issuer) -> None:
    await oidc.authenticate(_token())
    await oidc.authenticate(_token())
    assert issuer.fetches == 1
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer.keys = [_jwk(rotated, "rotated")]
    with pytest.raises(AuthenticationError, match="unknown key"):  # refreshes at most this often
        await oidc.authenticate(_token(key=rotated, kid="rotated"))
    assert issuer.fetches == 1
    oidc._fetched_at -= 60
    identity = await oidc.authenticate(_token(key=rotated, kid="rotated"))
    assert identity.principal == "user:alice@acme"
    assert issuer.fetches == 2


async def test_unknown_key_ids_can_not_hammer_the_issuer(oidc: Oidc, issuer: Issuer) -> None:
    await oidc.authenticate(_token())
    for attempt in range(5):
        with pytest.raises(AuthenticationError):
            await oidc.authenticate(_token(kid=f"made-up-{attempt}"))
    assert issuer.fetches == 1


async def test_a_token_is_refused_when_the_issuer_can_not_be_reached(
    oidc: Oidc, issuer: Issuer
) -> None:
    issuer.down = True
    with pytest.raises(AuthenticationError, match="can't be reached"):
        await oidc.authenticate(_token())


async def test_known_keys_keep_working_while_the_issuer_is_down(oidc: Oidc, issuer: Issuer) -> None:
    await oidc.authenticate(_token())
    issuer.down = True
    oidc._fetched_at -= 7200
    identity = await oidc.authenticate(_token())
    assert identity.tenant == "acme"


async def test_roles_can_come_from_a_nested_claim(
    issuer: Issuer, store: InMemorySecurityStore
) -> None:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(issuer.handle))
    settings = OidcSettings(ISSUER, AUDIENCE, roles_claim="realm_access.roles")
    oidc = Oidc(settings, store, http=http)
    identity = await oidc.authenticate(_token(roles=None, realm_access={"roles": ["operator", 7]}))
    assert identity.roles == {"operator"}
    await http.aclose()


async def test_a_token_without_roles_grants_none(oidc: Oidc) -> None:
    assert (await oidc.authenticate(_token(roles=None))).roles == frozenset()
    assert (await oidc.authenticate(_token(roles="member"))).roles == frozenset()


async def test_elliptic_curve_tokens_work(issuer: Issuer, store: InMemorySecurityStore) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    issuer.keys = [{**jwk, "kid": "ec", "use": "sig", "alg": "ES256"}]
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(issuer.handle))
    identity = await Oidc(OidcSettings(ISSUER, AUDIENCE), store, http=http).authenticate(
        _token(key=key, algorithm="ES256", kid="ec")
    )
    assert identity.tenant == "acme"
    await http.aclose()


def test_an_issuer_must_use_https_except_on_loopback(store: InMemorySecurityStore) -> None:
    with pytest.raises(ValueError, match="https"):
        Oidc(OidcSettings("http://id.example.test", AUDIENCE), store)
    Oidc(OidcSettings("http://localhost:8180/realms/aios", AUDIENCE), store)
    Oidc(OidcSettings("http://127.0.0.1:8180/realms/aios", AUDIENCE), store)


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("127.5.5.5", True),
        ("::1", True),
        ("0.0.0.0", False),  # noqa: S104
        ("::", False),
        ("10.0.0.1", False),
        ("example.com", False),
        ("localhost.example.com", False),
    ],
)
def test_loopback_means_this_machine_only(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback


async def test_credentials_route_by_shape(oidc: Oidc, store: InMemorySecurityStore) -> None:
    key, value = new_key("globex", "ci", ["member"], created_by="system:test")
    await store.add_key(key)
    credentials = Credentials(ApiKeys(store), oidc)
    assert (await credentials.authenticate(value)).tenant == "globex"
    assert (await credentials.authenticate(_token())).tenant == "acme"


async def test_without_an_issuer_a_token_is_just_an_unknown_key(
    store: InMemorySecurityStore,
) -> None:
    with pytest.raises(AuthenticationError):
        await Credentials(ApiKeys(store)).authenticate(_token())

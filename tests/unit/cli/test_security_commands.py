"""The security commands of the ``aios`` CLI, and the rules a server starts under (ADR-011)."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.cli import client as cli_client
from aios.cli.main import main
from aios.cli.server import authenticator_for, check_exposure, oidc_settings
from aios.controlplane import Runtime
from aios.fabric import InMemoryEventBus
from aios.security.identity import Credentials
from aios.security.secrets import EncryptedFileSecrets, new_store_key

Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> list[httpx2.Request]:
    """Every ``Client`` talks to a recording stand-in for the API."""
    seen: list[httpx2.Request] = []
    original = httpx2.Client

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx2.Response(204)
        if request.url.path == "/v1/audit/verify":
            ok = "bad" not in request.headers.get("Authorization", "")
            return httpx2.Response(200, json={"ok": ok, "entries": 3})
        if request.url.path == "/v1/keys" and request.method == "POST":
            return httpx2.Response(201, json={"id": "K", "key": "aios_K_secret"})
        return httpx2.Response(200, json=[])

    def client(**kwargs: Any) -> httpx2.Client:
        return original(transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(cli_client.httpx2, "Client", client)
    return seen


def test_the_key_in_the_environment_goes_on_every_request(
    server: list[httpx2.Request],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AIOS_API_KEY", "aios_K_abc")
    assert main(["audit", "list"]) == 0
    assert server[0].headers["Authorization"] == "Bearer aios_K_abc"
    assert "X-AIOS-Tenant" not in server[0].headers


def test_without_a_key_the_cli_sends_none(
    server: list[httpx2.Request],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("AIOS_API_KEY", raising=False)
    main(["keys", "list"])
    assert "Authorization" not in server[0].headers


@pytest.mark.parametrize(
    ("key", "code"), [("aios_K_good", 0), ("aios_K_bad", 1)], ids=["intact", "broken"]
)
def test_audit_verify_fails_the_shell_when_the_chain_is_broken(
    server: list[httpx2.Request],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    key: str,
    code: int,
) -> None:
    monkeypatch.setenv("AIOS_API_KEY", key)
    assert main(["audit", "verify"]) == code
    assert '"entries": 3' in capsys.readouterr().out


def test_a_new_key_goes_to_stdout_and_its_record_to_stderr(
    server: list[httpx2.Request], capsys: pytest.CaptureFixture[str]
) -> None:
    """So ``$key = aios keys create …`` captures the key and nothing else."""
    assert main(["keys", "create", "ci", "--role", "operator", "--expires-in-days", "7"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "aios_K_secret"
    assert "shown once" in captured.err
    assert json.loads(server[0].content) == {
        "name": "ci",
        "roles": ["operator"],
        "expires_in_days": 7,
    }


def test_keys_can_be_revoked(
    server: list[httpx2.Request], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["keys", "revoke", "K123"]) == 0
    assert (server[0].method, server[0].url.path) == ("DELETE", "/v1/keys/K123")


def test_tailing_events_says_why_it_was_refused() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, json={"detail": "no policy allows this (default deny)"})

    api = cli_client.Client("http://aios")
    api.http = httpx2.Client(base_url="http://aios", transport=httpx2.MockTransport(refuse))
    with pytest.raises(SystemExit, match="error 403: no policy allows this"):
        cli_client.tail(api, None, ">")


class _Stop(Exception):
    pass


def test_tailing_starts_at_the_newest_event_without_reading_the_log() -> None:
    asked: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        if len(asked) > 1:
            raise _Stop  # one look at what follows is enough
        return httpx2.Response(200, json=[{"position": 42, "type": "run.started"}])

    api = cli_client.Client("http://aios")
    api.http = httpx2.Client(base_url="http://aios", transport=httpx2.MockTransport(answer))
    with pytest.raises(_Stop):
        cli_client.tail(api, None, ">")
    first, then = (dict(request.url.params) for request in asked)
    assert (first["newest"], first["limit"]) == ("true", "1")
    assert then["after"] == "42"


def test_keys_need_a_name(server: list[httpx2.Request]) -> None:
    with pytest.raises(SystemExit, match="needs a name"):
        main(["keys", "create"])


def test_the_kill_switch_from_the_cli(
    server: list[httpx2.Request], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["suspend", "agent", "refund-agent", "--reason", "incident 7"]) == 0
    assert json.loads(server[0].content) == {
        "kind": "agent",
        "name": "refund-agent",
        "reason": "incident 7",
    }
    assert main(["unsuspend", "agent", "refund-agent"]) == 0
    assert server[1].url.path == "/v1/suspensions/agent/refund-agent"
    assert main(["suspensions"]) == 0


def test_platform_commands_need_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOS_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="needs --database-url"):
        main(["platform", "key", "--tenant", "acme", "--name", "ops"])


# --------------------------------------------------------------------------- secrets


def test_the_secret_store_key_can_be_generated(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["secrets", "new-key"]) == 0
    EncryptedFileSecrets("unused.json", capsys.readouterr().out.strip())


async def test_a_secret_set_on_the_command_line_can_be_read_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key, path = new_store_key(), tmp_path / "secrets.json"
    monkeypatch.setenv("AIOS_SECRETS_FILE", str(path))
    monkeypatch.setenv("AIOS_SECRETS_KEY", key)
    read, write = os.pipe()
    os.write(write, b"the-value\n")
    os.close(write)
    monkeypatch.setattr("sys.stdin", os.fdopen(read))
    assert main(["secrets", "set", "BILLING_KEY", "--tenant", "acme", "--app", "desk"]) == 0
    assert "the-value" not in capsys.readouterr().out
    assert await EncryptedFileSecrets(path, key).get("acme", "desk", "BILLING_KEY") == "the-value"


def test_setting_a_secret_needs_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOS_SECRETS_FILE", raising=False)
    with pytest.raises(SystemExit, match="needs AIOS_SECRETS_FILE"):
        main(["secrets", "set", "X", "--tenant", "a", "--app", "b"])


# --------------------------------------------------------------------------- how a server starts


@pytest.mark.parametrize(
    ("role", "host", "no_auth", "allowed"),
    [
        ("dev", "127.0.0.1", False, True),
        ("dev", "localhost", False, True),
        ("dev", "0.0.0.0", False, False),  # noqa: S104
        ("dev", "192.168.1.5", False, False),
        ("api", "0.0.0.0", False, True),  # noqa: S104 - it has sign-in
        ("api", "127.0.0.1", True, True),
        ("api", "0.0.0.0", True, False),  # noqa: S104
        ("worker", "0.0.0.0", False, True),  # noqa: S104 - it listens on nothing
    ],
)
def test_a_server_anyone_can_reach_always_has_sign_in(
    role: Any, host: str, no_auth: bool, allowed: bool
) -> None:
    if allowed:
        check_exposure(role, host, no_auth=no_auth)
    else:
        with pytest.raises(SystemExit, match="loopback"):
            check_exposure(role, host, no_auth=no_auth)


def test_only_the_api_role_signs_callers_in() -> None:
    runtime = Runtime(InMemoryEventBus())
    assert authenticator_for("dev", runtime, no_auth=False) is None
    assert authenticator_for("api", runtime, no_auth=True) is None
    assert isinstance(authenticator_for("api", runtime, no_auth=False), Credentials)


def test_oidc_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AIOS_OIDC_ISSUER", "AIOS_OIDC_AUDIENCE", "AIOS_OIDC_ROLES_CLAIM"):
        monkeypatch.delenv(name, raising=False)
    assert oidc_settings() is None
    monkeypatch.setenv("AIOS_OIDC_ISSUER", "https://id.example.test/realms/aios")
    with pytest.raises(SystemExit, match="AIOS_OIDC_AUDIENCE"):
        oidc_settings()
    monkeypatch.setenv("AIOS_OIDC_AUDIENCE", "aios-api")
    monkeypatch.setenv("AIOS_OIDC_ROLES_CLAIM", "realm_access.roles")
    settings = oidc_settings()
    assert settings is not None
    assert settings.roles_claim == "realm_access.roles"


def test_tls_needs_both_files(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="go together"):
        main(["dev", "--tls-cert", str(tmp_path / "c.pem")])


def test_a_refused_server_explains_itself_before_it_starts() -> None:
    with pytest.raises(SystemExit, match="loopback"):
        main(["dev", "--host", "0.0.0.0"])  # noqa: S104

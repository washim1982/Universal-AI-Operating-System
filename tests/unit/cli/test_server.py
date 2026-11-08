"""Stopping the server: an open connection doesn't get a veto."""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from aios.cli import server
from aios.core.policy import SideEffect
from aios.tools import NativeProvider, tool

REGISTRY = """
models:
  - id: google/<general-model>
    model: gemini-test
    prices: {input: 1.0, output: 2.0}
  - {id: private/llama, model: llama, deployment: private, prices: {input: 0.1, output: 0.1}}
"""


def test_a_deployment_can_fill_in_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text(REGISTRY, "utf-8")
    monkeypatch.setenv("AIOS_MODEL_REGISTRY", str(path))
    registry = server.model_registry()
    filled = registry.get("google/<general-model>")
    assert filled is not None
    assert (filled.model, filled.placeholder) == ("gemini-test", False)
    assert registry.get("private/llama") is not None
    assert registry.get("anthropic/claude-opus-5-5") is not None  # the rest stays


async def test_a_server_uses_the_registry_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text(REGISTRY, "utf-8")
    monkeypatch.setenv("AIOS_MODEL_REGISTRY", str(path))
    async with server.running(
        "api",
        apps=[],
        tenants=["acme"],
        database_url=None,
        cassettes=None,
        cassette_mode="replay",
    ) as (runtime, _):
        assert runtime.models.registry.get("private/llama") is not None


def test_a_deployment_can_add_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    @tool("desk.note.write", side_effect=SideEffect.WRITE)
    async def write(text: str) -> str:
        return text

    module = ModuleType("deployment_tools")
    module.providers = lambda: [NativeProvider("desk", [write])]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "deployment_tools", module)
    monkeypatch.setenv("AIOS_TOOL_PROVIDERS", "deployment_tools:providers")
    assert [provider.name for provider in server.tool_providers()] == ["desk"]


async def test_a_server_uses_the_tools_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    @tool("desk.note.write", side_effect=SideEffect.WRITE)
    async def write(text: str) -> str:
        return text

    module = ModuleType("deployment_tools")
    module.providers = lambda: [NativeProvider("desk", [write])]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "deployment_tools", module)
    monkeypatch.setenv("AIOS_TOOL_PROVIDERS", "deployment_tools:providers")
    async with server.running(
        "api",
        apps=[],
        tenants=["acme"],
        database_url=None,
        cassettes=None,
        cassette_mode="replay",
    ) as (runtime, _):
        assert "desk.note.write" in [spec.name for spec in runtime.tool_registry.specs()]


def test_with_none_named_there_are_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOS_TOOL_PROVIDERS", raising=False)
    assert server.tool_providers() == []


def test_a_mistyped_tool_provider_stops_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOS_TOOL_PROVIDERS", "no_colon_here")
    with pytest.raises(SystemExit, match="module:attribute"):
        server.tool_providers()


def test_without_one_the_shipped_registry_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOS_MODEL_REGISTRY", raising=False)
    filled = server.model_registry().get("google/<general-model>")
    assert filled is not None
    assert filled.placeholder


def test_a_registry_that_isnt_there_stops_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOS_MODEL_REGISTRY", str(tmp_path / "missing.yaml"))
    with pytest.raises(SystemExit, match="no such file"):
        server.model_registry()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _endless(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        raise NotImplementedError
    headers = [(b"content-type", b"text/event-stream")]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": b"data: hello\n\n", "more_body": True})
    await asyncio.Event().wait()  # a follower of a long run looks just like this


async def test_an_open_stream_cant_hold_up_a_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SHUTDOWN_SECONDS", 1)
    port = _free_port()
    web = server.web_server(_endless, host="127.0.0.1", port=port)
    web.config.lifespan = "off"
    running = asyncio.create_task(web.serve())
    for _ in range(500):
        if web.started:
            break
        await asyncio.sleep(0.01)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
    await writer.drain()
    while b"hello" not in await reader.readline():
        pass
    web.should_exit = True
    await asyncio.wait_for(running, 5)
    writer.close()


CHANNEL_SETTINGS = (
    "AIOS_SLACK_BOT_TOKEN",
    "AIOS_TEAMS_WORKFLOW_URL",
    "AIOS_SMTP_HOST",
    "AIOS_SMTP_TLS",
    "AIOS_SMTP_PORT",
    "AIOS_EMAIL_FROM",
    "AIOS_ACTION_LINK_SECRET",
    "AIOS_PUBLIC_URL",
)


@pytest.fixture
def no_channels(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in CHANNEL_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_no_channels_unless_asked(no_channels: pytest.MonkeyPatch) -> None:
    assert server.approval_channels() == ([], None)


def test_channels_from_the_environment(no_channels: pytest.MonkeyPatch) -> None:
    no_channels.setenv("AIOS_SLACK_BOT_TOKEN", "xoxb-test")
    no_channels.setenv("AIOS_TEAMS_WORKFLOW_URL", "https://teams.example/flow")
    no_channels.setenv("AIOS_SMTP_HOST", "smtp.example")
    no_channels.setenv("AIOS_SMTP_TLS", "ssl")
    no_channels.setenv("AIOS_EMAIL_FROM", "aios@example.com")
    no_channels.setenv("AIOS_ACTION_LINK_SECRET", "x" * 32)
    no_channels.setenv("AIOS_PUBLIC_URL", "https://aios.example/")
    channels, links = server.approval_channels()
    assert [c.name for c in channels] == ["slack", "teams", "email"]
    assert links is not None
    assert links.base_url == "https://aios.example"
    email = channels[2]
    assert getattr(email, "deliver").port == 465  # noqa: B009 - ssl's port unless told


@pytest.mark.parametrize(
    ("settings", "complaint"),
    [
        ({"AIOS_SLACK_BOT_TOKEN": "xoxb-test"}, "AIOS_PUBLIC_URL and AIOS_ACTION_LINK_SECRET"),
        (
            {
                "AIOS_SLACK_BOT_TOKEN": "xoxb-test",
                "AIOS_PUBLIC_URL": "https://aios.example",
                "AIOS_ACTION_LINK_SECRET": "short",
            },
            "at least 32",
        ),
        ({"AIOS_SMTP_HOST": "smtp.example"}, "AIOS_EMAIL_FROM"),
        (
            {"AIOS_SMTP_HOST": "smtp.example", "AIOS_EMAIL_FROM": "a@b.c", "AIOS_SMTP_TLS": "yes"},
            "starttls, ssl or none",
        ),
    ],
)
def test_a_channel_setting_that_cant_work_stops_the_server(
    no_channels: pytest.MonkeyPatch, settings: dict[str, str], complaint: str
) -> None:
    for name, value in settings.items():
        no_channels.setenv(name, value)
    with pytest.raises(SystemExit, match=complaint):
        server.approval_channels()


@pytest.mark.parametrize(("value", "seconds"), [(None, 120.0), ("30", 30.0), ("0.5", 0.5)])
def test_how_long_a_client_waits_for_an_answer(
    monkeypatch: pytest.MonkeyPatch, value: str | None, seconds: float
) -> None:
    if value is None:
        monkeypatch.delenv("AIOS_ANSWER_WAIT", raising=False)
    else:
        monkeypatch.setenv("AIOS_ANSWER_WAIT", value)
    assert server.answer_wait() == seconds


@pytest.mark.parametrize("value", ["0", "-5", "soon", "901"])
def test_a_wait_that_makes_no_sense_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AIOS_ANSWER_WAIT", value)
    with pytest.raises(SystemExit, match="AIOS_ANSWER_WAIT"):
        server.answer_wait()

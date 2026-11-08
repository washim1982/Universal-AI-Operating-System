"""AIOS Apps as an MCP server's tools, to the MCP SDK's own client (FR-INT-01 P1; WP 3.7)."""

import contextlib
import importlib
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx2
import mcp
import pytest
from mcp.client.streamable_http import streamable_http_client

from aios.api import create_app
from aios.controlplane import load_app
from aios.core.identity import Identity
from aios.core.ids import new_ulid
from aios.core.manifests import load_manifests
from aios.fabric import Scope
from aios.security.identity import ApiKeys
from aios.testing.runtime import LocalRuntime
from aios.tools import ToolProvider

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "tests" / "fixtures" / "apps" / "helper"
OPERATOR = "system:platform"
LISTENER = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: listener, version: 0.1.0}
spec:
  entrypoints: [{type: event, on: "acme.orders.placed", workflow: noted}]
  workflows: [noted]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: noted, version: 0.1.0}
spec:
  steps:
    - {id: wait, type: wait, signal: never}
"""


class Api:
    def __init__(self, runtime: LocalRuntime, app: Any) -> None:
        self.runtime = runtime
        self.app = app

    async def key(self, name: str, *roles: str, tenant: str = "acme") -> str:
        _, value = await self.runtime.runtime.create_key(
            Identity(OPERATOR, tenant), name, list(roles)
        )
        return value

    @contextlib.asynccontextmanager
    async def client(
        self, key: str | None = None, *, host: str = "aios.example", mode: str = "auto"
    ) -> AsyncGenerator[mcp.Client]:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), headers=headers, timeout=30
        )
        transport = streamable_http_client(f"http://{host}/mcp", http_client=http)
        async with http, mcp.Client(transport, mode=mode, read_timeout_seconds=30) as client:
            yield client


@contextlib.asynccontextmanager
async def helper_runtime(**api: Any) -> AsyncGenerator[Api]:
    """The helper App in tenant acme, and the API in front of it, running; globex has no Apps."""

    async with LocalRuntime(tenants=["acme", "globex"]) as runtime:
        await runtime.runtime.apply(load_app(HELPER), tenant="acme")
        await runtime.runtime.apply(load_manifests(LISTENER), tenant="acme")
        stand_ins: list[ToolProvider] = importlib.import_module("helper_app.tools").providers()
        for provider in stand_ins:  # importable once the App is loaded
            await runtime.runtime.register_tool_provider(provider)
        api.setdefault("authenticator", ApiKeys(runtime.runtime.security))
        app = create_app(runtime.runtime, **api)
        async with app.router.lifespan_context(app):  # the MCP sessions run in the lifespan
            yield Api(runtime, app)


def _state(result: Any) -> dict[str, Any]:
    return dict(result.structured_content or {})


def _text(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


async def test_an_mcp_client_sees_the_apps_and_asks_one() -> None:
    async with helper_runtime(answer_wait=10) as api:
        async with api.client(await api.key("alice", "member")) as client:
            assert client.server_info is not None
            assert client.server_info.name == "aios"
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert sorted(tools) == ["ask-helper", "run-status"]  # nothing to approve with
            ask = tools["ask-helper"]
            assert ask.description is not None
            assert ask.description.startswith("Answers questions, and refunds orders")
            assert ask.input_schema["required"] == ["message"]
            assert tools["run-status"].annotations is not None
            assert tools["run-status"].annotations.read_only_hint is True

            result = await client.call_tool("ask-helper", {"message": "hi"})
            assert result.is_error is False
            assert _text(result) == "you said hi (0 messages)"
            answer = _state(result)
            assert answer["status"] == "completed"
            assert answer["output"] == {"answer": "you said hi (0 messages)"}
            assert answer["trace"] == f"/v1/apps/helper/runs/{answer['run_id']}"

            [resource] = (await client.list_resources()).resources
            assert str(resource.uri) == "aios://apps/helper"
            read = await client.read_resource("aios://apps/helper")
            described = json.loads(getattr(read.contents[0], "text", "{}"))
            assert described["app"] == "helper"

        async for stored in api.runtime.bus.replay(
            Scope("acme", "helper"), "app.request.received", run_id=str(answer["run_id"])
        ):
            assert str(stored.event.onbehalfof).startswith("service:alice")  # the caller's run
            assert stored.event.data["via"] == "mcp"  # type: ignore[index]


async def test_a_run_that_waits_for_a_person_is_asked_about_again() -> None:
    async with (
        helper_runtime(answer_wait=10) as api,
        api.client(await api.key("alice", "member")) as client,
    ):
        waiting = await client.call_tool("ask-helper", {"message": "refund"})
        state = _state(waiting)
        assert state["status"] == "waiting"
        run = str(state["run_id"])
        [approval] = await api.runtime.runtime.human.store.in_run("acme", run)
        assert f"approval {approval.id}" in _text(waiting)
        assert f"ask again with run-status, app helper, run_id {run}" in _text(waiting)

        lead = Identity("user:lee@acme", "acme", frozenset({"member", "lead"}))
        await api.runtime.runtime.decide_approval(lead, approval.id, approve=True)
        done = await client.call_tool(
            "run-status", {"app": "helper", "run_id": run, "wait_seconds": 10}
        )
        assert _state(done)["status"] == "completed"
        assert _text(done) == "refunded"


async def test_run_status_waits_as_long_as_it_is_asked_to() -> None:
    async with (
        helper_runtime(answer_wait=10) as api,
        api.client(await api.key("alice", "member")) as client,
    ):
        run = await api.runtime.runtime.chat("helper", "slow", tenant="acme")
        at_once = await client.call_tool("run-status", {"app": "helper", "run_id": run})
        assert _state(at_once)["status"] == "running"
        assert f"ask again with run-status, app helper, run_id {run}" in _text(at_once)
        later = await client.call_tool(
            "run-status", {"app": "helper", "run_id": run, "wait_seconds": 10}
        )
        assert (_state(later)["status"], _text(later)) == ("completed", "done at last")


async def test_a_failed_run_is_a_tool_error() -> None:
    async with helper_runtime(answer_wait=10) as api:
        async with api.client(await api.key("alice", "member")) as client:
            result = await client.call_tool("ask-helper", {"message": "fail"})
        assert result.is_error is True
        assert "refused on purpose" in _text(result)


async def test_who_may_ask_and_what() -> None:
    async with helper_runtime(answer_wait=10) as api:
        with pytest.raises(Exception):  # noqa: B017, PT011 - the client can't even connect
            async with api.client() as client:
                await client.list_tools()
        async with api.client(await api.key("audrey", "auditor")) as auditor:
            refused = await auditor.call_tool("ask-helper", {"message": "hi"})
            assert refused.is_error is True
            assert _text(refused).startswith("not allowed")
        async with api.client(await api.key("alice", "member")) as alice:
            for name, arguments, says in [
                ("ask-listener", {"message": "hi"}, "no App 'listener' to ask"),
                ("ask-nobody", {"message": "hi"}, "no App 'nobody' to ask"),
                ("approve", {"id": "x"}, "no tool 'approve'"),
                ("ask-helper", {"message": "hi", "session": "nope"}, "session is"),
                ("run-status", {"app": "helper", "run_id": "nope"}, "app is an App's name"),
                ("run-status", {"app": "helper", "run_id": new_ulid()}, "no run"),
            ]:
                result = await alice.call_tool(name, arguments)
                assert result.is_error is True, name
                assert says in _text(result), (name, _text(result))
        async with api.client(await api.key("gil", "member", tenant="globex")) as other:
            assert [t.name for t in (await other.list_tools()).tools] == ["run-status"]
            result = await other.call_tool("ask-helper", {"message": "hi"})
            assert result.is_error is True


async def test_a_local_server_answers_only_local_names() -> None:
    async with helper_runtime(authenticator=None) as api:
        async with api.client(host="127.0.0.1:8000") as client:
            assert "ask-helper" in [t.name for t in (await client.list_tools()).tools]
        with pytest.raises(Exception):  # noqa: B017, PT011 - DNS rebinding protection
            async with api.client(host="attacker.example") as client:
                await client.list_tools()


async def test_a_client_of_the_classic_protocol_is_served_too() -> None:
    async with (
        helper_runtime(answer_wait=10) as api,
        api.client(await api.key("alice", "member"), mode="legacy") as client,
    ):
        assert "ask-helper" in [t.name for t in (await client.list_tools()).tools]
        result = await client.call_tool("ask-helper", {"message": "json"})
        assert _state(result)["output"] == {"total": 3, "items": ["a", "b"]}

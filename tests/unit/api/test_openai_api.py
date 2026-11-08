"""An App, spoken to as an OpenAI model, by OpenAI's own client (FR-INT-03, FR-APP-02; WP 3.7)."""

import contextlib
import importlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from openai import AsyncOpenAI

from aios.api import create_app
from aios.api import openai_compat as openai_api
from aios.controlplane import load_app
from aios.controlplane.asking import outcome
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


@dataclass
class Api:
    runtime: LocalRuntime
    app: Any

    async def key(self, name: str, *roles: str, tenant: str = "acme") -> str:
        _, value = await self.runtime.runtime.create_key(
            Identity(OPERATOR, tenant), name, list(roles)
        )
        return value

    def client(self, key: str, **headers: str) -> AsyncOpenAI:
        transport = httpx2.ASGITransport(app=self.app)
        return AsyncOpenAI(
            base_url="http://aios/openai/v1",
            api_key=key,
            max_retries=0,
            default_headers=headers or None,
            http_client=httpx2.AsyncClient(transport=transport),
        )

    async def request_of(self, run: str) -> Any:
        async for stored in self.runtime.bus.replay(
            Scope("acme", "helper"), "app.request.received", run_id=run
        ):
            return stored.event
        raise AssertionError(f"no request for run {run}")


@contextlib.asynccontextmanager
async def helper_runtime(**api: Any) -> AsyncGenerator[tuple[LocalRuntime, Any]]:
    """The helper App in tenant acme, and the API in front of it; globex has no Apps."""

    async with LocalRuntime(tenants=["acme", "globex"]) as runtime:
        await runtime.runtime.apply(load_app(HELPER), tenant="acme")
        await runtime.runtime.apply(load_manifests(LISTENER), tenant="acme")
        stand_ins: list[ToolProvider] = importlib.import_module("helper_app.tools").providers()
        for provider in stand_ins:  # importable once the App is loaded
            await runtime.runtime.register_tool_provider(provider)
        authenticator = ApiKeys(runtime.runtime.security)
        yield runtime, create_app(runtime.runtime, authenticator=authenticator, **api)


@pytest.fixture
async def api() -> AsyncGenerator[Api]:
    async with helper_runtime(answer_wait=10) as (runtime, app):
        yield Api(runtime, app)


CONVERSATION: list[Any] = [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "Hi!"},
    {
        "role": "user",
        "content": [{"type": "text", "text": "what is "}, {"type": "text", "text": "new"}],
    },
]


async def test_an_openai_client_talks_to_an_app(api: Api) -> None:
    client = api.client(await api.key("alice", "member"))
    listed = [model.id async for model in client.models.list()]
    assert listed == ["helper"]  # an App without a chat entry point isn't one to talk to
    assert (await client.models.retrieve("helper")).owned_by == "aios"

    reply = await client.chat.completions.create(model="helper", messages=CONVERSATION)
    [choice] = reply.choices
    assert choice.message.role == "assistant"
    assert choice.message.content == "you said what is new (4 messages)"
    assert choice.finish_reason == "stop"
    assert reply.model == "helper"
    extra = reply.model_extra or {}
    run = extra["aios"]["run_id"]
    assert reply.id == f"chatcmpl-{run}"
    assert extra["aios"] == {
        "run_id": run,
        "status": "completed",
        "trace": f"/v1/apps/helper/runs/{run}",
    }
    assert reply.usage is not None
    assert reply.usage.total_tokens == 0  # the agent used no model

    request = await api.request_of(run)
    assert str(request.onbehalfof).startswith("service:alice")
    assert request.data["input"]["message"] == "what is new"
    assert request.data["via"] == "openai"
    assert request.data["input"]["messages"][0] == {"role": "system", "content": "Be brief."}


async def test_streaming_sends_the_answer_when_it_is_there(api: Api) -> None:
    client = api.client(await api.key("alice", "member"))
    stream = await client.chat.completions.create(
        model="helper",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks = [chunk async for chunk in stream]
    assert len({chunk.id for chunk in chunks}) == 1  # one completion, one run
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert text == "you said hi (1 messages)"
    finishes = [c.choices[0].finish_reason for c in chunks if c.choices]
    assert finishes[-1] == "stop"
    assert chunks[-1].choices == []
    assert chunks[-1].usage is not None


async def test_a_streamed_run_that_works_long_keeps_the_connection(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openai_api, "KEEPALIVE_SECONDS", 0.1)
    key = await api.key("alice", "member")
    transport = httpx2.ASGITransport(app=api.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
        response = await http.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "helper",
                "messages": [{"role": "user", "content": "slow"}],
                "stream": True,
            },
        )
    assert response.headers["content-type"].startswith("text/event-stream")
    assert ": the run is still working" in response.text
    assert '"content": "done at last"' in response.text
    assert response.text.endswith("data: [DONE]\n\n")


async def test_a_run_that_waits_for_a_person_says_so(api: Api) -> None:
    client = api.client(await api.key("alice", "member"))
    reply = await client.chat.completions.create(
        model="helper", messages=[{"role": "user", "content": "refund"}]
    )
    extra = (reply.model_extra or {})["aios"]
    assert extra["status"] == "waiting"
    text = reply.choices[0].message.content or ""
    [approval] = await api.runtime.runtime.human.store.in_run("acme", extra["run_id"])
    assert f"approval {approval.id}" in text
    assert "from role:lead" in text
    assert f"ask about run {extra['run_id']} again later" in text


async def test_a_run_longer_than_the_wait_goes_on() -> None:
    async with helper_runtime(answer_wait=0.1) as (runtime, app):
        api = Api(runtime, app)
        client = api.client(await api.key("alice", "member"))
        reply = await client.chat.completions.create(
            model="helper", messages=[{"role": "user", "content": "slow"}]
        )
        extra = (reply.model_extra or {})["aios"]
        assert extra["status"] == "running"
        assert "still working" in (reply.choices[0].message.content or "")
        alice = Identity("service:alice", "acme", frozenset({"member"}))
        later = await outcome(api.runtime.runtime, alice, "helper", extra["run_id"], timeout=10)
        assert (later.status, later.text) == ("completed", "done at last")


async def test_an_answer_that_is_data_comes_back_as_json(api: Api) -> None:
    client = api.client(await api.key("alice", "member"))
    reply = await client.chat.completions.create(
        model="helper", messages=[{"role": "user", "content": "json"}]
    )
    assert reply.choices[0].message.content == (
        '```json\n{\n  "total": 3,\n  "items": [\n    "a",\n    "b"\n  ]\n}\n```'
    )


async def test_a_failed_run_is_an_error(api: Api) -> None:
    client = api.client(await api.key("alice", "member"))
    with pytest.raises(openai.InternalServerError) as raised:
        await client.chat.completions.create(
            model="helper", messages=[{"role": "user", "content": "fail"}]
        )
    assert raised.value.status_code == 502
    assert "refused on purpose" in raised.value.message
    stream = await client.chat.completions.create(
        model="helper", messages=[{"role": "user", "content": "fail"}], stream=True
    )
    with pytest.raises(openai.APIError, match="refused on purpose"):
        _ = [chunk async for chunk in stream]  # the run's failure comes as the stream's error


async def test_who_may_ask_and_what(api: Api) -> None:
    alice = api.client(await api.key("alice", "member"))
    hello = [{"role": "user", "content": "hi"}]
    with pytest.raises(openai.AuthenticationError):
        await api.client("aios_not-a-key").chat.completions.create(model="helper", messages=hello)  # type: ignore[arg-type]
    with pytest.raises(openai.AuthenticationError):
        await api.client("aios_not-a-key").models.list()
    auditor = api.client(await api.key("audrey", "auditor"))
    assert [m.id async for m in auditor.models.list()] == ["helper"]
    with pytest.raises(openai.PermissionDeniedError):
        await auditor.chat.completions.create(model="helper", messages=hello)  # type: ignore[arg-type]
    for model in ("nobody", "listener"):
        with pytest.raises(openai.NotFoundError) as raised:
            await alice.chat.completions.create(model=model, messages=hello)  # type: ignore[arg-type]
        assert raised.value.code == "model_not_found"
    other = api.client(await api.key("gil", "member", tenant="globex"))
    with pytest.raises(openai.NotFoundError):
        await other.chat.completions.create(model="helper", messages=hello)  # type: ignore[arg-type]
    assert [m.id async for m in other.models.list()] == []


async def test_what_it_refuses(api: Api) -> None:
    alice = api.client(await api.key("alice", "member"))
    hello: Any = [{"role": "user", "content": "hi"}]
    weather: Any = {"type": "function", "function": {"name": "weather", "parameters": {}}}
    with pytest.raises(openai.BadRequestError) as raised:
        await alice.chat.completions.create(model="helper", messages=hello, tools=[weather])
    assert raised.value.code == "tools_not_supported"
    with pytest.raises(openai.BadRequestError, match="no user message"):
        await alice.chat.completions.create(
            model="helper", messages=[{"role": "system", "content": "x"}]
        )
    with pytest.raises(openai.BadRequestError, match="not a chat completion"):
        await alice.chat.completions.create(model="helper", messages=[])
    with pytest.raises(openai.BadRequestError, match="X-AIOS-Session"):
        await alice.with_options(
            default_headers={"X-AIOS-Session": "nope"}
        ).chat.completions.create(model="helper", messages=hello)
    with pytest.raises(openai.BadRequestError, match="X-AIOS-Data-Class"):
        await alice.with_options(
            default_headers={"X-AIOS-Data-Class": "secret-ish"}
        ).chat.completions.create(model="helper", messages=hello)


async def test_a_conversation_and_its_label_carry_over(api: Api) -> None:
    session = new_ulid()
    alice = api.client(
        await api.key("alice", "member"),
        **{"X-AIOS-Session": session, "X-AIOS-Data-Class": "confidential"},
    )
    reply = await alice.chat.completions.create(
        model="helper", messages=[{"role": "user", "content": "hi"}]
    )
    request = await api.request_of((reply.model_extra or {})["aios"]["run_id"])
    assert request.correlationid == session
    assert request.dataclass.value == "confidential"

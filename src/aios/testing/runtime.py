"""An in-process AIOS for acceptance tests (WP 1.7): the real kernel, engine, Model Kernel and
Tool OS on one event loop, with recorded model cassettes instead of live providers.

    async with LocalRuntime.from_app("apps/ai-researcher", cassettes="tests/cassettes/x") as rt:
        result = await (await rt.chat("ai-researcher", "Why?")).wait(timeout=120)

``durable=True`` runs on PostgreSQL when ``AIOS_TEST_DATABASE_URL`` is set (the kill test then
crosses a real database), and on the in-memory fabric otherwise; either way the fabric outlives
the worker, so :meth:`LocalRuntime.kill_worker` and :meth:`restart_worker` exercise recovery.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from aios.controlplane import Runtime, load_app
from aios.core.cassettes import Cassette, CassetteMode
from aios.core.catalog import ModelCallCompleted, Usage
from aios.core.envelope import Event
from aios.core.ids import new_ulid
from aios.core.manifests import ModelPolicyManifest, ModelRule
from aios.fabric import EventBus, InMemoryEventBus, Scope
from aios.kernel import Context, agent, definition_of
from aios.kernel.definitions import Handler
from aios.models import Provider
from aios.testing.mcp import FakeMCPServer, fake_mcp_server
from aios.tools import MCPProvider, ToolProvider

__all__ = [
    "FakeMCPServer",
    "LocalRuntime",
    "RunHandle",
    "RunResult",
    "fake_mcp_server",
    "ping_pong_agents",
]

TERMINAL = ("run.completed", "run.failed", "run.cancelled")
DEFAULT_TENANT = "acme"
AGENTS_APP = "local-agents"


@dataclass
class RunResult:
    status: Literal["completed", "failed", "cancelled"]
    output: Any
    usage: Usage
    events: list[Event]
    """The run's events, up to and including the event that ended it."""
    cassette: Cassette | None
    error: dict[str, Any] | None = None

    def model_calls(self, *, step: str | None = None) -> list[ModelCallCompleted]:
        calls = [
            ModelCallCompleted.model_validate(e.data)
            for e in self.events
            if e.type == "model.call.completed"
        ]
        return [c for c in calls if step is None or c.step == step]


class RunHandle:
    def __init__(self, runtime: LocalRuntime, app: str, run_id: str) -> None:
        self.runtime = runtime
        self.app = app
        self.run_id = run_id

    async def events(self) -> list[Event]:
        return await self.runtime.runtime.events(self.app, self.run_id)

    async def wait_for_event(self, type: str, timeout: float = 60.0, **data: Any) -> Event:
        """The first event of ``type`` whose data contains every ``data`` item."""
        async with asyncio.timeout(timeout):
            while True:
                for event in await self.events():
                    payload = event.data if isinstance(event.data, dict) else {}
                    if event.type == type and all(payload.get(k) == v for k, v in data.items()):
                        return event
                await asyncio.sleep(0.02)

    async def wait(self, timeout: float = 120.0) -> RunResult:
        async with asyncio.timeout(timeout):
            while True:
                events = await self.events()
                end = next((i for i, e in enumerate(events) if e.type in TERMINAL), None)
                if end is not None:
                    return self._result(events[: end + 1])
                await asyncio.sleep(0.02)

    def _result(self, events: list[Event]) -> RunResult:
        last = events[-1]
        data: dict[str, Any] = last.data if isinstance(last.data, dict) else {}
        status: Literal["completed", "failed", "cancelled"] = last.type.split(".")[1]  # type: ignore[assignment]
        return RunResult(
            status=status,
            output=data.get("output"),
            usage=Usage.model_validate(data.get("usage") or {}),
            events=events,
            cassette=self.runtime.cassette,
            error=data.get("error"),
        )


class LocalRuntime:
    def __init__(
        self,
        *,
        agents: Sequence[Handler] = (),
        cassettes: str | Path | None = None,
        cassette_mode: CassetteMode | None = None,
        durable: bool = False,
        tenant: str = DEFAULT_TENANT,
        lease: float = 2.0,
        providers: Mapping[str, Provider] | None = None,
        tool_providers: Sequence[ToolProvider] | None = None,
    ) -> None:
        mode: CassetteMode = cassette_mode or os.environ.get("AIOS_CASSETTE_MODE", "replay")  # type: ignore[assignment]
        self.cassette = Cassette.open(cassettes, mode) if cassettes is not None else None
        self.durable = durable
        self.tenant = tenant
        self._bus: EventBus | None = None
        self._lease = lease
        self._agents = [definition_of(handler) for handler in agents]
        self._app_dirs: list[Path] = []
        self._pending: list[ToolProvider] = list(tool_providers or [])
        self._providers = providers
        self.runtime: Runtime = None  # type: ignore[assignment] - set in __aenter__

    @classmethod
    def from_app(cls, path: str | Path, **options: Any) -> LocalRuntime:
        runtime = cls(**options)
        runtime._app_dirs.append(Path(path))
        return runtime

    @property
    def bus(self) -> EventBus:
        assert self._bus is not None, "use LocalRuntime as an async context manager"
        return self._bus

    async def __aenter__(self) -> Self:
        self._bus = await self._make_bus()
        self.runtime = Runtime(
            self._bus,
            tenant=self.tenant,
            cassette=self.cassette,
            providers=self._providers,
            lease=self._lease,
            sandbox=Path(os.environ.get("TMP", ".")) / f"aios-sandbox-{new_ulid().lower()}",
        )
        for directory in self._app_dirs:
            self.runtime.install(load_app(directory))
        if self._agents:
            self.runtime.install_agents(self._agents, app=AGENTS_APP)
        await self.runtime.start()
        await self._bind_pending()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.runtime.stop()
        await self.bus.close()
        if self.cassette is not None and self.cassette.recording:
            self.cassette.save()

    async def _make_bus(self) -> EventBus:
        url = os.environ.get("AIOS_TEST_DATABASE_URL")
        if self.durable and url:
            from aios.fabric.postgres import PostgresEventBus  # noqa: PLC0415 - optional

            bus = PostgresEventBus(url, schema=f"test_{new_ulid().lower()}", drop_on_close=True)
            await bus.connect()
            return bus
        return InMemoryEventBus()

    # ------------------------------------------------------------------ requests

    async def chat(self, app: str, message: str) -> RunHandle:
        await self._bind_pending()
        return RunHandle(self, app, await self.runtime.chat(app, message))

    async def publish_and_collect(
        self, type: str, *, until: str, data: Any = None, timeout: float = 30.0
    ) -> list[Event]:
        """Publish one fact and collect everything that follows, until ``until`` appears."""
        stream = await self.bus.subscribe(Scope(self.tenant, AGENTS_APP), ">")
        first = Event(
            source=f"aios://{self.tenant}/apps/{AGENTS_APP}/test",
            type=type,
            tenantid=self.tenant,
            appid=AGENTS_APP,
            principal=f"user:tester@{self.tenant}",
            data=data,
        )
        await self.bus.publish(first)
        seen: list[Event] = []
        async with asyncio.timeout(timeout), stream:
            while not seen or seen[-1].type != until:
                delivery = await stream.next()
                assert delivery is not None, "the bus closed"
                await delivery.ack()
                if not delivery.event.type.startswith("agent.lifecycle."):
                    seen.append(delivery.event)
        return seen

    # ------------------------------------------------------------------ configuration

    def register_tool_provider(
        self, name: str, *, mcp_url: str | None = None, provider: ToolProvider | None = None
    ) -> None:
        """One config entry adds a tool provider; it is bound before the next request."""
        if provider is None:
            if mcp_url is None:
                raise ValueError("pass mcp_url or provider")
            provider = MCPProvider(name, mcp_url)
        self._pending.append(provider)

    async def _bind_pending(self) -> None:
        while self._pending:
            await self.runtime.register_tool_provider(self._pending.pop(0))

    def apply_model_policy_rule(
        self, *, task: str, candidates: Sequence[str], policy: str = "default"
    ) -> None:
        """Edit the model policy as an operator would edit its YAML (M1 exit criterion 3)."""
        current = self.runtime.models.policies[policy]
        rule = ModelRule.model_validate({"match": {"task": task}, "candidates": list(candidates)})
        rules = [rule, *(r for r in current.spec.rules if r.match.task != task)]
        updated = current.model_copy(
            update={"spec": current.spec.model_copy(update={"rules": rules})}
        )
        self.runtime.set_model_policy(ModelPolicyManifest.model_validate(updated.model_dump()))

    # ------------------------------------------------------------------ crashes

    async def kill_worker(self) -> None:
        await self.runtime.kill_workers()

    async def restart_worker(self) -> None:
        await self.runtime.start_workers()


@agent("ping-agent", subscribe="ping.sent")
async def _ping(ctx: Context, event: Event) -> None:
    await ctx.emit("pong.sent")


@agent("pong-agent", subscribe="pong.sent")
async def _pong(ctx: Context, event: Event) -> None:
    await ctx.emit("ping.sent")


def ping_pong_agents() -> list[Handler]:
    """Two agents that trigger each other forever, until the hop limit stops them."""
    return [_ping, _pong]

"""The composition root: one AIOS installation in one process (ADR-001).

``Runtime`` wires the fabric, the Agent Kernel, the Workflow Engine, the Model Kernel and the
Tool OS together through their ports, and installs Apps from their manifests. ``aios dev``,
the API and the in-process test runtime all use it; production splits it into an API process
and worker processes that share a PostgreSQL fabric.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from aios.core.cassettes import Cassette
from aios.core.envelope import Event
from aios.core.errors import NotFoundError
from aios.core.ids import new_ulid
from aios.core.manifests import (
    AgentManifest,
    AnyManifest,
    AppManifest,
    ModelPolicyManifest,
    PolicyManifest,
    ToolAccess,
    ToolStep,
    WorkflowManifest,
    load_manifests,
)
from aios.core.policy import PolicyDecisionPoint
from aios.fabric.port import EventBus, Scope
from aios.kernel import (
    AgentDefinition,
    Context,
    Installation,
    Worker,
    agent,
    definition_of,
    from_manifest,
)
from aios.models import ModelKernel, ModelRegistry, Provider
from aios.tools import (
    MCPProvider,
    StaticToolPolicy,
    ToolProvider,
    ToolRegistry,
    ToolService,
    filesystem_provider,
    http_provider,
    search_provider,
)
from aios.workflow import TOOL_CAPABILITY, AppDefinition, WorkflowCatalog, WorkflowEngine

logger = logging.getLogger("aios.controlplane")

TOOL_RUNNER = "tool-runner"


@agent(TOOL_RUNNER, capability=TOOL_CAPABILITY)
async def run_tool(ctx: Context, task: dict[str, Any]) -> Any:
    """The kernel's built-in agent for ``type: tool`` workflow steps."""
    return await ctx.tools.invoke(
        str(task["tool"]), idempotency_key=ctx.step_key("tool"), **dict(task.get("input") or {})
    )


@dataclass
class InstalledApp:
    tenant: str
    manifest: AppManifest
    agents: list[AgentDefinition]
    workflows: dict[str, WorkflowManifest]
    policies: list[PolicyManifest] = field(default_factory=list[PolicyManifest])

    @property
    def name(self) -> str:
        return self.manifest.metadata.name


def load_app(path: str | Path, config: Mapping[str, Any] | None = None) -> list[AnyManifest]:
    """Every manifest of an App directory (``manifests/`` and ``policies/``).

    ``${config.*}`` placeholders take the defaults of ``config.schema.json`` unless ``config``
    overrides them (FR-APP-07). An App with code under ``src/`` is made importable.
    """
    root = Path(path)
    if not (root / "manifests").is_dir():
        raise FileNotFoundError(f"{root} is not an App directory (no manifests/ folder)")
    defaults: dict[str, Any] = {}
    schema = root / "config.schema.json"
    if schema.exists():
        properties = json.loads(schema.read_text(encoding="utf-8")).get("properties", {})
        defaults = {k: v["default"] for k, v in properties.items() if "default" in v}
    values = {**defaults, **(config or {})}
    if schema.exists():  # FR-APP-07: the install's configuration must match the App's schema
        try:
            jsonschema.validate(values, json.loads(schema.read_text(encoding="utf-8")))
        except jsonschema.ValidationError as exc:
            raise ValueError(f"{root.name}: invalid configuration: {exc.message}") from exc
    manifests: list[AnyManifest] = []
    for folder in ("manifests", "policies"):
        for file in sorted((root / folder).glob("*.yaml")):
            manifests += load_manifests(file, config=values)
    source = root / "src"
    if source.is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))
    return manifests


class Runtime:
    def __init__(
        self,
        bus: EventBus,
        *,
        tenant: str = "local",
        cassette: Cassette | None = None,
        registry: ModelRegistry | None = None,
        providers: Mapping[str, Provider] | None = None,
        tool_providers: Sequence[ToolProvider] | None = None,
        sandbox: str | Path | None = None,
        policy: PolicyDecisionPoint | None = None,
        lease: float = 30.0,
    ) -> None:
        self.bus = bus
        self.tenant = tenant
        self.cassette = cassette
        self.lease = lease
        self.models = ModelKernel(bus, registry=registry, providers=providers, cassette=cassette)
        self.tool_registry = ToolRegistry()
        self.tool_policy = StaticToolPolicy({})
        self.tools = ToolService(
            bus, self.tool_registry, policy or self.tool_policy, cassette=cassette
        )
        self._tool_providers = list(
            tool_providers
            if tool_providers is not None
            else [
                search_provider(),
                http_provider(),
                filesystem_provider(sandbox or Path.cwd() / ".aios" / "sandbox"),
            ]
        )
        self.catalog = WorkflowCatalog()
        self.apps: dict[tuple[str, str], InstalledApp] = {}
        self.engine: WorkflowEngine | None = None
        self.workers: list[Worker] = []
        self._started = False

    # ------------------------------------------------------------------ apps

    def install(
        self, manifests: Sequence[AnyManifest], *, tenant: str | None = None
    ) -> InstalledApp:
        """Install one App (its App manifest plus its agents, workflows and policies)."""
        tenant = tenant or self.tenant
        apps = [m for m in manifests if isinstance(m, AppManifest)]
        if len(apps) != 1:
            raise ValueError(f"expected exactly one App manifest, found {len(apps)}")
        [app] = apps
        agents = {m.metadata.name: m for m in manifests if isinstance(m, AgentManifest)}
        workflows = {m.metadata.name: m for m in manifests if isinstance(m, WorkflowManifest)}
        for name in app.spec.agents:
            if name not in agents:
                raise NotFoundError(f"App {app.metadata.name}: no Agent manifest for {name!r}")
        for name in app.spec.workflows:
            if name not in workflows:
                raise NotFoundError(f"App {app.metadata.name}: no Workflow manifest for {name!r}")
        definitions = [from_manifest(agents[name]) for name in app.spec.agents]
        tool_steps = sorted(
            {s.tool for w in workflows.values() for s in w.spec.steps if isinstance(s, ToolStep)}
        )
        if tool_steps:
            definitions.append(definition_of(run_tool))
            self.tool_policy.grant(app.metadata.name, TOOL_RUNNER, ToolAccess(allow=tool_steps))
        for definition in definitions:
            if definition.name != TOOL_RUNNER:
                self.tool_policy.grant(app.metadata.name, definition.name, definition.tools)
        for policy in manifests:
            if isinstance(policy, ModelPolicyManifest):
                self.models.set_policy(policy)
        self.models.use_policy(tenant, app.metadata.name, app.spec.model_policy)
        capabilities = frozenset(c for d in definitions for c in d.capabilities)
        previous = self.catalog.get(tenant, app.metadata.name)
        versions = {
            **(previous.versions if previous is not None else {}),
            **{(w.metadata.name, w.metadata.version): w for w in workflows.values()},
        }
        self.catalog.install(tenant, AppDefinition(app, workflows, capabilities, versions))
        installed = InstalledApp(
            tenant,
            app,
            definitions,
            workflows,
            [m for m in manifests if isinstance(m, PolicyManifest)],
        )
        self.apps[(tenant, app.metadata.name)] = installed
        return installed

    def install_agents(
        self, definitions: Sequence[AgentDefinition], *, app: str, tenant: str | None = None
    ) -> None:
        """Host agents without an App manifest (choreography, tests)."""
        tenant = tenant or self.tenant
        manifest = AppManifest.model_validate(
            {
                "metadata": {"name": app, "version": "0.0.0"},
                "spec": {
                    "entrypoints": [{"type": "event", "on": ">", "workflow": "none"}],
                    "workflows": ["none"],
                },
            }
        )
        self.apps[(tenant, app)] = InstalledApp(tenant, manifest, list(definitions), {})

    def app(self, name: str, *, tenant: str | None = None) -> InstalledApp:
        installed = self.apps.get((tenant or self.tenant, name))
        if installed is None:
            raise NotFoundError(f"App {name!r} is not installed")
        return installed

    # ------------------------------------------------------------------ tools and models

    async def register_tool_provider(self, provider: ToolProvider) -> list[str]:
        """Bind a provider's tools; replaces an earlier provider of the same name (FR-INT-01)."""
        return [spec.name for spec in await self.tool_registry.bind(provider)]

    async def register_mcp_server(self, name: str, url: str) -> list[str]:
        """One config entry adds an MCP server's tools (M1 exit criterion 4)."""
        return await self.register_tool_provider(MCPProvider(name, url))

    def set_model_policy(self, policy: ModelPolicyManifest) -> None:
        self.models.set_policy(policy)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._started:
            return
        for provider in self._tool_providers:
            await self.tool_registry.bind(provider)
        await self.start_workers()
        self._started = True

    async def start_workers(self) -> None:
        """Start the engine and one kernel worker per installed App (the "worker process")."""
        self.engine = WorkflowEngine(self.bus, self.catalog, lease=self.lease)
        await self.engine.start()
        self.workers = [
            Worker(
                self.bus,
                models=self.models,
                tools=self.tools,
                installations=[Installation(app.tenant, app.name, app.agents)],
                lease=self.lease,
            )
            for app in self.apps.values()
            if app.agents
        ]
        for worker in self.workers:
            await worker.start()

    async def apply(
        self, manifests: Sequence[AnyManifest], *, tenant: str | None = None
    ) -> InstalledApp:
        """Install an App into a running runtime: its agents start serving immediately."""
        installed = self.install(manifests, tenant=tenant)
        definition = self.catalog.get(installed.tenant, installed.name)
        if self.engine is not None and definition is not None:
            await self.engine.watch_entrypoints(installed.tenant, definition)
        if self._started and installed.agents:
            worker = Worker(
                self.bus,
                models=self.models,
                tools=self.tools,
                installations=[Installation(installed.tenant, installed.name, installed.agents)],
                lease=self.lease,
            )
            await worker.start()
            self.workers.append(worker)
        return installed

    async def kill_workers(self) -> None:
        """Crash the worker side (engine and kernel) without settling anything."""
        if self.engine is not None:
            await self.engine.kill()
        for worker in self.workers:
            await worker.kill()
        self.workers = []

    async def stop(self) -> None:
        if self.engine is not None:
            await self.engine.stop()
        for worker in self.workers:
            await worker.stop()
        await self.tool_registry.close()
        self._started = False

    # ------------------------------------------------------------------ requests

    async def chat(
        self,
        app: str,
        message: str | None = None,
        *,
        input: Any = None,
        tenant: str | None = None,
        principal: str = "user:anonymous",
        traceparent: str | None = None,
    ) -> str:
        """Start a run through the App's chat entrypoint; returns the run id (FR-APP-02)."""
        installed = self.app(app, tenant=tenant)
        run_id = new_ulid()
        await self.bus.publish(
            Event(
                source=f"aios://{installed.tenant}/apps/{app}/entrypoints/chat",
                type="app.request.received",
                tenantid=installed.tenant,
                appid=app,
                runid=run_id,
                correlationid=run_id,
                principal=principal,
                traceparent=traceparent,
                data={
                    "entrypoint": "chat",
                    "input": input if input is not None else {"message": message},
                },
            )
        )
        return run_id

    async def cancel(self, app: str, run_id: str, *, tenant: str | None = None) -> None:
        installed = self.app(app, tenant=tenant)
        await self.bus.publish(
            Event(
                source=f"aios://{installed.tenant}/apps/{app}/api",
                type="run.cancel.requested",
                tenantid=installed.tenant,
                appid=app,
                runid=run_id,
                correlationid=run_id,
                principal="user:anonymous",
                data={"reason": "cancelled by request"},
            )
        )

    async def events(self, app: str, run_id: str, *, tenant: str | None = None) -> list[Event]:
        scope = Scope(tenant or self.tenant, app)
        return [stored.event async for stored in self.bus.replay(scope, run_id=run_id)]

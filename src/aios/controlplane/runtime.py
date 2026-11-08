"""The composition root: one AIOS installation in one process (ADR-001).

``Runtime`` wires the fabric, the Agent Kernel, the Workflow Engine, the Model Kernel and the
Tool OS together through their ports, and installs Apps from their manifests. ``aios dev``,
the API and the in-process test runtime all use it; production splits it into an API process
and worker processes that share a PostgreSQL fabric.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import jsonschema
from pydantic import BaseModel, Field, JsonValue

from aios.adapters import handler_for
from aios.controlplane import replay
from aios.controlplane.bundle import BundleError, code_folder
from aios.controlplane.evals import Evaluator
from aios.controlplane.installations import InMemoryInstallationStore, InstallationStore
from aios.controlplane.installer import Installer
from aios.controlplane.reconciler import Reconciler
from aios.core.apps import Bundle, DataChoice, Grants
from aios.core.apps import Installation as AppInstallation
from aios.core.cassettes import Cassette
from aios.core.catalog import (
    AppRequestReceived,
    ContactChanged,
    FeedbackGiven,
    GroupChanged,
    InjectionSuspected,
    KeyChanged,
    PolicyDecided,
    SuspensionChanged,
)
from aios.core.envelope import DataClass, Event
from aios.core.errors import (
    ApprovalForbiddenError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PolicyDeniedError,
    ToolNotFoundError,
)
from aios.core.human import Approval, HumanTask
from aios.core.identity import ApiKey, Identity, SecurityStore, Suspendable, Suspension
from aios.core.ids import new_ulid
from aios.core.manifests import (
    AgentManifest,
    AnyManifest,
    AppManifest,
    Egress,
    EvalCase,
    HumanCapability,
    ModelPolicyManifest,
    PolicyManifest,
    ScheduleEntrypoint,
    ToolAccess,
    ToolStep,
    WebhookEntrypoint,
    WorkflowManifest,
    load_manifests,
    manifest_files,
)
from aios.core.policy import (
    PolicyAction,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyRequest,
    Resource,
)
from aios.core.ports import InjectionClassifier, SecretStore, screened
from aios.core.schedule import Cron
from aios.core.topics import PLATFORM_APP_ID
from aios.fabric.port import EventBus, Scope
from aios.fabric.postgres import PostgresEventBus
from aios.human import (
    ActionLink,
    ActionLinks,
    ApprovalNotifier,
    ApprovalStore,
    ApprovalWebhook,
    Channel,
    Contact,
    HumanService,
    signed_by,
    tasks_for,
)
from aios.human import store_for as approval_store_for
from aios.kernel import (
    AgentDefinition,
    Context,
    Installation,
    Worker,
    agent,
    definition_of,
    from_manifest,
)
from aios.memory import Document, MemoryService, MemoryStore
from aios.memory import store_for as memory_store_for
from aios.models import ModelKernel, ModelRegistry, Provider, ledger_for
from aios.security.audit import AuditLog, AuditRecorder
from aios.security.enforcement import Enforcer, consent_policy
from aios.security.identity import new_key
from aios.security.injection import HeuristicClassifier
from aios.security.secrets import EnvSecrets
from aios.security.store import stores_for
from aios.tools import (
    MCPProvider,
    NativeProvider,
    ToolProvider,
    ToolRegistry,
    ToolService,
    ToolSpec,
    filesystem_provider,
    http_provider,
    search_provider,
)
from aios.workflow import (
    TOOL_CAPABILITY,
    AppDefinition,
    RunState,
    WorkflowCatalog,
    WorkflowEngine,
    fold,
)

logger = logging.getLogger("aios.controlplane")

RUN_LIST_MAX = 200
SCHEDULE_SECONDS = 20.0
WEBHOOK_WINDOW = 300.0
UNSIGNED = "that request isn't signed, or is too old"
TICK_MEMORY = timedelta(hours=1)  # longer than any request can stay fresh, or a clock can drift
TOOL_RUNNER = "tool-runner"
ANONYMOUS = "user:anonymous"
EXPIRY_SECONDS = 60.0
_RUN_STATUS = {
    "run.started": "running",
    "run.resumed": "running",
    "run.paused": "paused",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}


@agent(TOOL_RUNNER, capability=TOOL_CAPABILITY)
async def run_tool(ctx: Context, task: dict[str, Any]) -> Any:
    """The kernel's built-in agent for ``type: tool`` workflow steps."""
    return await ctx.tools.invoke(
        str(task["tool"]), idempotency_key=ctx.step_key("tool"), **dict(task.get("input") or {})
    )


@dataclass(frozen=True, slots=True)
class Feedback:
    event: Event
    """The ``human.feedback.given`` event."""
    memory: str | None = None
    """The memory item it was kept as, if it was."""

    @property
    def payload(self) -> FeedbackGiven:
        return FeedbackGiven.model_validate(self.event.data)

    def view(self) -> dict[str, JsonValue]:
        return {
            "feedback": self.event.id,
            "run": self.event.runid,
            "at": self.event.time.isoformat(),
            **self.payload.model_dump(mode="json"),
            "memory": self.memory,
        }


@dataclass
class InstalledApp:
    tenant: str
    manifest: AppManifest
    agents: list[AgentDefinition]
    workflows: dict[str, WorkflowManifest]
    policies: list[PolicyManifest] = field(default_factory=list[PolicyManifest])
    grants: Grants | None = None
    """What a tenant admin agreed to. ``None`` for an App a process was started with."""

    @property
    def name(self) -> str:
        return self.manifest.metadata.name


def _granted_egress(egress: Egress, grants: Grants | None) -> Egress:
    """An App's egress lists, without the hosts a tenant admin left out."""
    if grants is None:
        return egress
    hosts = set(grants.hosts)
    return egress.model_copy(
        update={
            "allow": None if egress.allow is None else [h for h in egress.allow if h in hosts],
            "tools": {
                tool: [host for host in allowed if host in hosts]
                for tool, allowed in egress.tools.items()
            },
        }
    )


def installation_store_for(bus: EventBus) -> InstallationStore:
    """The installation store for a fabric: its own database, or memory."""
    if isinstance(bus, PostgresEventBus):
        from aios.controlplane.postgres import PostgresInstallationStore  # noqa: PLC0415

        return PostgresInstallationStore(bus)
    return InMemoryInstallationStore(bus)


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
        for file in manifest_files(root / folder):
            manifests += load_manifests(file, config=values)
    source = root / "src"
    if source.is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))
    return manifests


class _DeclaredSecrets(SecretStore):
    """An App's tools get only the secrets its manifest declares (``spec.config.secrets``)."""

    def __init__(self, runtime: Runtime, store: SecretStore) -> None:
        self.runtime = runtime
        self.store = store

    async def get(self, tenant: str, app: str, name: str) -> str | None:
        installed = self.runtime.apps.get((tenant, app))
        if installed is None or name not in installed.manifest.spec.config.secrets:
            return None
        return await self.store.get(tenant, app, name)


class RunSummary(BaseModel):
    """One line of a run list. :meth:`Runtime.run_state` has the rest."""

    run_id: str
    app: str
    status: str
    workflow: str | None = None
    started_at: datetime | None = None
    updated_at: datetime
    on_behalf_of: str | None = None
    tainted: bool = False
    cost_usd: float = 0.0
    source_run: str | None = Field(default=None, description="What this replays, forks or retries")
    mode: str | None = None


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
        secrets: SecretStore | None = None,
        security: SecurityStore | None = None,
        audit: AuditLog | None = None,
        installations: InstallationStore | None = None,
        app_cache: Path | None = None,
        memory: MemoryStore | None = None,
        approvals: ApprovalStore | None = None,
        approval_webhook: ApprovalWebhook | None = None,
        approval_channels: Sequence[Channel] = (),
        action_links: ActionLinks | None = None,
        classifier: InjectionClassifier | None = None,
        lease: float = 30.0,
    ) -> None:
        self.bus = bus
        self.tenant = tenant
        self.cassette = cassette
        self.lease = lease
        stores = stores_for(bus)
        self.security = security or stores[0]
        self.audit = audit or stores[1]
        self.classifier = classifier or HeuristicClassifier()
        self.enforcer = Enforcer(self.security)
        self.policy = policy or self.enforcer
        self.models = ModelKernel(
            bus,
            registry=registry,
            providers=providers,
            cassette=cassette,
            decisions=self.policy,
            ledger=ledger_for(bus),
        )
        self.memory = MemoryService(
            bus, memory or memory_store_for(bus), policy=self.policy, models=self.models
        )
        approval_store = approvals or approval_store_for(bus)
        if approval_channels and action_links is None:
            raise ValueError("approval channels need action links to send")
        self.links = action_links
        notifier = (
            ApprovalNotifier(
                approval_store,
                approval_channels,
                action_links,
                groups=self.groups,
                contacts=self.contacts,
                holders=self.security.holders,
            )
            if approval_channels and action_links is not None
            else None
        )
        self.human = HumanService(
            bus,
            approval_store,
            webhook=approval_webhook,
            lease=lease,
            groups=self.groups,
            notifier=notifier,
            tasks=tasks_for(bus),
            humans=self.humans,
        )
        self.tool_registry = ToolRegistry()
        self.secrets = _DeclaredSecrets(self, secrets or EnvSecrets())
        self.tools = ToolService(
            bus,
            self.tool_registry,
            self.policy,
            cassette=cassette,
            secrets=self.secrets,
            approvals=self.human,
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
        self.installations = installations or installation_store_for(bus)
        self.installer = Installer(self.installations, prove=self._prove)
        self.app_cache = app_cache or Path.cwd() / ".aios" / "apps"
        self.reconciler = Reconciler(bus, self.installations, self, cache=self.app_cache)
        self.evaluator = Evaluator(self, lease=self.lease)
        self.engine: WorkflowEngine | None = None
        self.workers: list[Worker] = []
        self.recorder: AuditRecorder | None = None
        self._janitor: asyncio.Task[None] | None = None
        self._scheduler: asyncio.Task[None] | None = None
        self._started = False

    # ------------------------------------------------------------------ apps

    def install(
        self,
        manifests: Sequence[AnyManifest],
        *,
        tenant: str | None = None,
        grants: Grants | None = None,
        requested: Grants | None = None,
        earlier: Sequence[WorkflowManifest] = (),
    ) -> InstalledApp:
        """Install one App (its App manifest plus its agents, workflows and policies).

        ``grants`` limits it to what a tenant admin agreed to, out of what it ``requested``.
        ``earlier`` holds other versions' workflows, for runs that started on them.
        """
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
        definitions = [
            from_manifest(agents[name], handler_for(agents[name])) for name in app.spec.agents
        ]
        access = {definition.name: definition.tools for definition in definitions}
        tool_steps = sorted(
            {s.tool for w in workflows.values() for s in w.spec.steps if isinstance(s, ToolStep)}
        )
        if tool_steps:
            definitions.append(definition_of(run_tool))
            access[TOOL_RUNNER] = ToolAccess(allow=tool_steps)
        consent = (
            [consent_policy(app.metadata.name, requested, grants)]
            if grants is not None and requested is not None
            else []
        )
        self.enforcer.install(
            tenant,
            app.metadata.name,
            policies=[*manifests, *consent],
            agents=access,
            memory={definition.name: definition.memory for definition in definitions},
        )
        for policy in manifests:
            if isinstance(policy, ModelPolicyManifest):
                self.models.set_policy(policy)
        self.models.use_policy(tenant, app.metadata.name, app.spec.model_policy)
        self.models.use_budgets(tenant, app.metadata.name, app.spec.budgets)
        self.tools.set_egress(tenant, app.metadata.name, _granted_egress(app.spec.egress, grants))
        by_agents = frozenset(c for d in definitions for c in d.capabilities)
        by_people = frozenset(h.capability for h in app.spec.humans)
        if both := sorted(by_agents & by_people):
            raise ValueError(
                f"App {app.metadata.name}: {', '.join(both)} can't be both an agent's and people's"
            )
        capabilities = by_agents | by_people
        previous = self.catalog.get(tenant, app.metadata.name)
        versions = {
            **(previous.versions if previous is not None else {}),
            **{(w.metadata.name, w.metadata.version): w for w in earlier},
            **{(w.metadata.name, w.metadata.version): w for w in workflows.values()},
        }
        classes = None if grants is None else frozenset(grants.data_classes)
        self.catalog.install(tenant, AppDefinition(app, workflows, capabilities, versions, classes))
        installed = InstalledApp(
            tenant,
            app,
            definitions,
            workflows,
            [m for m in manifests if isinstance(m, PolicyManifest)],
            grants,
        )
        self.apps[(tenant, app.metadata.name)] = installed
        return installed

    def _prove(self, bundle: Bundle, manifests: Sequence[AnyManifest]) -> None:
        """Refuse a version whose agents can't be loaded, with the reason."""
        code_folder(bundle, self.app_cache)
        try:
            for manifest in manifests:
                if isinstance(manifest, AgentManifest):
                    from_manifest(manifest, handler_for(manifest))
        except (ImportError, AttributeError, ValueError) as exc:
            raise BundleError(f"{bundle.name} {bundle.version} won't load: {exc}") from exc

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
        self.enforcer.install(
            tenant,
            app,
            policies=(),
            agents={definition.name: definition.tools for definition in definitions},
            memory={definition.name: definition.memory for definition in definitions},
        )

    def app(self, name: str, *, tenant: str | None = None) -> InstalledApp:
        installed = self.apps.get((tenant or self.tenant, name))
        if installed is None:
            raise NotFoundError(f"App {name!r} is not installed")
        return installed

    # ------------------------------------------------------------------ tools and models

    async def register_tool_provider(self, provider: ToolProvider) -> list[str]:
        """Bind a provider's tools; replaces an earlier provider of the same name (FR-INT-01)."""
        return [spec.name for spec in await self.tool_registry.bind(provider)]

    async def tool_spec(self, name: str, tenant: str) -> ToolSpec | None:
        """What a tool is, even in a process that serves none, such as the API.

        The built-in providers are bound only where tools run, so they are asked here directly.
        That costs nothing for a native provider; an MCP server would need a connection, so its
        tools are known only where they are bound.
        """
        try:
            return self.tool_registry.resolve(name, tenant).spec
        except ToolNotFoundError:
            pass
        for provider in self._tool_providers:
            if isinstance(provider, NativeProvider):
                found = [spec for spec in await provider.list_tools() if spec.name == name]
                if found:
                    return found[0]
        return None

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
        await self.reconciler.start()

    async def start_workers(self) -> None:
        """Start the engine and one kernel worker per installed App (the "worker process")."""
        await self.human.start()  # first, so that it hears every request a recovering run makes
        self.engine = WorkflowEngine(self.bus, self.catalog, lease=self.lease)
        await self.engine.start()
        self.recorder = AuditRecorder(self.bus, self.audit, lease=self.lease)
        await self.recorder.start()
        await self.evaluator.start()
        self._janitor = asyncio.create_task(self._expire_memory())
        self._scheduler = asyncio.create_task(self._run_schedules())
        self.workers = [self._worker(app) for app in self.apps.values() if app.agents]
        for worker in self.workers:
            await worker.start()

    async def _expire_memory(self) -> None:
        """Reads already skip what has expired; this gives the space back."""
        while True:
            await asyncio.sleep(EXPIRY_SECONDS)
            try:
                await self.memory.expire()
            except Exception:
                logger.exception("could not expire memory; will try again")

    async def _stop_janitor(self) -> None:
        for task in (self._janitor, self._scheduler):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._janitor = self._scheduler = None

    def _worker(self, app: InstalledApp) -> Worker:
        return Worker(
            self.bus,
            models=self.models,
            tools=self.tools,
            memory=self.memory,
            policy=self.policy,
            classifier=self.classifier,
            installations=[Installation(app.tenant, app.name, app.agents)],
            lease=self.lease,
        )

    async def apply(
        self,
        manifests: Sequence[AnyManifest],
        *,
        tenant: str | None = None,
        grants: Grants | None = None,
        requested: Grants | None = None,
        earlier: Sequence[WorkflowManifest] = (),
    ) -> InstalledApp:
        """Install an App into a running runtime, or replace it: its agents serve immediately."""
        installed = self.install(
            manifests, tenant=tenant, grants=grants, requested=requested, earlier=earlier
        )
        definition = self.catalog.get(installed.tenant, installed.name)
        if self.engine is not None and definition is not None:
            await self.engine.watch_entrypoints(installed.tenant, definition)
        if self._started:
            await self._stop_workers_of(installed.tenant, installed.name)
            if installed.agents:
                worker = self._worker(installed)
                await worker.start()
                self.workers.append(worker)
        return installed

    async def remove(self, tenant: str, app: str) -> None:
        """Take an App out of this process."""
        if self.engine is not None:
            await self.engine.unwatch_entrypoints(tenant, app)
        await self._stop_workers_of(tenant, app)
        self.catalog.remove(tenant, app)
        self.enforcer.remove(tenant, app)
        self.apps.pop((tenant, app), None)

    async def _stop_workers_of(self, tenant: str, app: str) -> None:
        for worker in [w for w in self.workers if w.serves(tenant, app)]:
            await worker.stop()
            self.workers.remove(worker)

    async def uninstall(self, by: Identity, app: str, data: DataChoice) -> AppInstallation:
        """Stop an App's runs and remove it, with what becomes of its data (FR-APP-05)."""
        if (by.tenant, app) in self.apps:
            for run in await self.runs(app, tenant=by.tenant, limit=RUN_LIST_MAX):
                if run.status in ("running", "paused"):
                    await self.cancel(app, run.run_id, tenant=by.tenant, principal=by.principal)
        removed = await self.installer.uninstall(by, app, data)
        if data == "delete":
            await self.memory.erase_app(by, app)
        await self.reconciler.sync(by.tenant, app)
        return removed

    async def kill_workers(self) -> None:
        """Crash the worker side (engine and kernel) without settling anything."""
        if self.engine is not None:
            await self.engine.kill()
        if self.recorder is not None:
            await self.recorder.stop()
        await self.human.stop()
        await self._stop_janitor()
        for worker in self.workers:
            await worker.kill()
        self.workers = []

    async def stop(self) -> None:
        await self.reconciler.stop()
        if self.engine is not None:
            await self.engine.stop()
        for worker in self.workers:
            await worker.stop()
        if self.recorder is not None:
            await self.recorder.stop()
        await self.evaluator.stop()
        await self.human.stop()
        await self._stop_janitor()
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
        principal: str = ANONYMOUS,
        data_class: DataClass = DataClass.INTERNAL,
        session: str | None = None,
        traceparent: str | None = None,
        via: Literal["openai", "mcp"] | None = None,
    ) -> str:
        """Start a run through the App's chat entrypoint; returns the run id (FR-APP-02).

        An authenticated caller becomes the run's delegate: every event of the run names it in
        ``onbehalfof``, and each tool call is decided for the agent and for it (FR-SEC-05).
        ``data_class`` labels the run, which limits the models it can reach (FR-SEC-11).
        ``session`` joins an earlier run's conversation, and with it its session memory.
        ``via`` says the request came through the OpenAI-compatible endpoint or the MCP server.

        A caller's own request doesn't taint its run: policy already limits it to what that caller
        may do, and otherwise every run would be tainted and taint would mean nothing. It does if
        the App says its callers pass on someone else's words, or if it looks like an injection.
        """
        installed = self.app(app, tenant=tenant)
        if installed.grants is not None and data_class not in installed.grants.data_classes:
            raise PolicyDeniedError(f"{app} wasn't granted {data_class.value} data")
        run_id = new_ulid()
        body: Any = input if input is not None else {"message": message}
        suspected = await screened(self.classifier, body, "input")
        relayed = any(
            entry.type == "chat" and entry.untrusted_input
            for entry in installed.manifest.spec.entrypoints
        )
        request = Event(
            source=f"aios://{installed.tenant}/apps/{app}/entrypoints/chat",
            type="app.request.received",
            tenantid=installed.tenant,
            appid=app,
            runid=run_id,
            correlationid=session or run_id,
            principal=principal,
            onbehalfof=None if principal == ANONYMOUS else principal,
            dataclass=data_class,
            tainted=relayed or suspected is not None,
            traceparent=traceparent,
            data={"entrypoint": "chat", "input": body, **({"via": via} if via else {})},
        )
        await self.bus.publish(request, *self._suspicion(request, suspected))
        return run_id

    @staticmethod
    def _suspicion(request: Event, suspected: InjectionSuspected | None) -> list[Event]:
        if suspected is None:
            return []
        return [
            request.derive(
                "security.injection.suspected",
                suspected.model_dump(mode="json"),
                source=f"aios://{request.tenantid}/system/security",
                principal="system:security",
            )
        ]

    async def _command(
        self,
        app: str,
        run_id: str,
        type: str,
        data: dict[str, Any],
        *,
        tenant: str | None,
        principal: str,
    ) -> None:
        installed = self.app(app, tenant=tenant)
        await self.bus.publish(
            Event(
                source=f"aios://{installed.tenant}/apps/{app}/api",
                type=type,
                tenantid=installed.tenant,
                appid=app,
                runid=run_id,
                correlationid=run_id,
                principal=principal,
                data=data,
            )
        )

    async def restart(
        self,
        app: str,
        run_id: str,
        mode: replay.Mode,
        *,
        tenant: str | None = None,
        principal: str = ANONYMOUS,
        step: str | None = None,
        input: Any = replay.KEEP,
        step_input: Any = None,
        model: str | None = None,
    ) -> str:
        """Replay, fork or retry a finished run, as a new run (FR-OBS-04, FR-WF-11, ADR-021)."""
        installed = self.app(app, tenant=tenant)
        request, state = await replay.request_for(
            self,
            installed.tenant,
            app,
            run_id,
            mode,
            step=step,
            input=input,
            step_input=step_input,
            model=model,
        )
        allowed = installed.grants.data_classes if installed.grants is not None else None
        if allowed is not None and state.data_class not in allowed:
            raise PolicyDeniedError(f"{app} wasn't granted {state.data_class.value} data")
        new = new_ulid()
        await self.bus.publish(
            Event(
                source=f"aios://{installed.tenant}/apps/{app}/{mode}",
                type="app.request.received",
                tenantid=installed.tenant,
                appid=app,
                runid=new,
                correlationid=new,
                principal=principal,
                onbehalfof=None if principal == ANONYMOUS else principal,
                dataclass=state.data_class,
                tainted=state.tainted,
                data=request.model_dump(mode="json"),
            )
        )
        return new

    async def _fire(
        self,
        app: str,
        tenant: str,
        entrypoint: Literal["schedule", "webhook"],
        workflow: str,
        body: Any,
        *,
        tainted: bool,
    ) -> str:
        """Start a run that nobody signed in to ask for (FR-APP-02)."""
        run_id = new_ulid()
        suspected = await screened(self.classifier, body, "input")
        payload = AppRequestReceived(entrypoint=entrypoint, workflow=workflow, input=body)
        request = Event(
            source=f"aios://{tenant}/apps/{app}/entrypoints/{entrypoint}",
            type="app.request.received",
            tenantid=tenant,
            appid=app,
            runid=run_id,
            correlationid=run_id,
            principal=f"system:{entrypoint}",
            tainted=tainted or suspected is not None,
            data=payload.model_dump(mode="json"),
        )
        await self.bus.publish(request, *self._suspicion(request, suspected))
        return run_id

    async def fire_schedules(self, now: datetime) -> list[str]:
        """Start whatever is due this minute. Of all the workers, one wins each tick."""
        tick = now.strftime("%Y-%m-%dT%H:%M")
        started: list[str] = []
        for (tenant, app), installed in list(self.apps.items()):
            for index, entry in enumerate(installed.manifest.spec.entrypoints):
                if (
                    isinstance(entry, ScheduleEntrypoint)
                    and Cron.parse(entry.cron).fires_at(now)
                    and await self.installations.claim(tenant, app, index, tick)
                ):
                    started.append(
                        await self._fire(
                            app, tenant, "schedule", entry.workflow, entry.input, tainted=False
                        )
                    )
        await self.installations.forget_ticks(now - TICK_MEMORY)
        return started

    async def _run_schedules(self) -> None:
        while True:
            await asyncio.sleep(SCHEDULE_SECONDS)
            try:
                await self.fire_schedules(datetime.now(UTC))
            except Exception:
                logger.exception("could not fire schedules; will try again")

    async def receive_webhook(
        self, tenant: str, app: str, name: str, body: bytes, header: str | None
    ) -> str:
        """Start a run from a signed request. It has no credential: the signature is its proof."""
        installed = self.apps.get((tenant, app))
        entries = enumerate(installed.manifest.spec.entrypoints) if installed else ()
        found = [
            (index, entry)
            for index, entry in entries
            if isinstance(entry, WebhookEntrypoint) and entry.name == name
        ]
        if not found:  # answered like a bad signature: nobody signed in, so nothing is confirmed
            raise AuthenticationError(UNSIGNED)
        [(index, hook)] = found
        secret = await self.secrets.get(tenant, app, hook.secret)
        if secret is None:
            logger.warning("webhook %s of %s has no secret %s", name, app, hook.secret)
        window = WEBHOOK_WINDOW
        proof = None if secret is None else signed_by(secret, header, body, tolerance=window)
        if proof is None:
            raise AuthenticationError(UNSIGNED)
        if not await self.installations.claim(tenant, app, index, f"hook:{proof}"):
            raise ConflictError("that request has been received already")
        try:
            parsed: Any = json.loads(body) if body else {}
        except ValueError:
            parsed = {"body": body.decode("utf-8", "replace")}
        return await self._fire(
            app, tenant, "webhook", hook.workflow, parsed, tainted=hook.untrusted_input
        )

    async def cancel(
        self, app: str, run_id: str, *, tenant: str | None = None, principal: str = ANONYMOUS
    ) -> None:
        data = {"reason": "cancelled by request"}
        await self._command(
            app, run_id, "run.cancel.requested", data, tenant=tenant, principal=principal
        )

    async def pause(
        self, app: str, run_id: str, *, tenant: str | None = None, principal: str = ANONYMOUS
    ) -> None:
        data = {"reason": "paused by request"}
        await self._command(
            app, run_id, "run.pause.requested", data, tenant=tenant, principal=principal
        )

    async def resume(
        self, app: str, run_id: str, *, tenant: str | None = None, principal: str = ANONYMOUS
    ) -> None:
        data = {"reason": "resumed by request"}
        await self._command(
            app, run_id, "run.resume.requested", data, tenant=tenant, principal=principal
        )

    async def signal(
        self,
        app: str,
        run_id: str,
        name: str,
        data: Any = None,
        *,
        tenant: str | None = None,
        principal: str = ANONYMOUS,
    ) -> None:
        """External input for a run; it waits in the log until a wait step takes it (FR-WF-08)."""
        payload = {"name": name, "data": data}
        await self._command(
            app, run_id, "run.signal.requested", payload, tenant=tenant, principal=principal
        )

    # ------------------------------------------------------------------ the Security Kernel

    async def authorize(
        self, identity: Identity, action: PolicyAction, *, app: str = PLATFORM_APP_ID
    ) -> PolicyDecision:
        """The decision for a control-plane action. A denial is recorded (FR-SEC-04, 08)."""
        decision = await self.policy.decide(
            PolicyRequest(
                tenant=identity.tenant,
                app=app,
                actor=identity.actor,
                action=action,
                resource=Resource.tenant(identity.tenant),
            )
        )
        if not decision.allowed:
            payload = PolicyDecided(
                action=action.value,
                resource=identity.tenant,
                policies=list(decision.policies),
                reason=decision.reason,
            )
            await self._security_event(identity, "policy.decision.denied", payload)
        return decision

    async def decide_approval(
        self,
        by: Identity,
        approval_id: str,
        *,
        approve: bool,
        comment: str | None = None,
        any_approver: bool = False,
        edits: dict[str, JsonValue] | None = None,
        via: str | None = None,
    ) -> Approval:
        """A person's answer. The kill switch applies, and a refusal is on the record."""
        approval = await self.human.get(by.tenant, approval_id)
        if approval is None:
            raise NotFoundError(f"no approval {approval_id}")
        decision = await self.authorize(by, PolicyAction.APPROVAL_DECIDE, app=approval.app)
        if not decision.allowed:
            raise PolicyDeniedError(decision.reason or "approval.decide is not allowed")
        try:
            return await self.human.decide(
                by,
                approval_id,
                approve=approve,
                comment=comment,
                any_approver=any_approver,
                edits=edits,
                via=via,
            )
        except ApprovalForbiddenError as exc:
            payload = PolicyDecided(
                action=PolicyAction.APPROVAL_DECIDE.value, resource=approval_id, reason=str(exc)
            )
            await self._security_event(by, "policy.decision.denied", payload)
            raise

    async def groups(self, tenant: str) -> dict[str, frozenset[str]]:
        """A tenant's groups and their members, as last set (FR-HUM-04). An empty group is gone."""
        found: dict[str, frozenset[str]] = {}
        scope = Scope(tenant, PLATFORM_APP_ID)
        async for stored in self.bus.replay(scope, "security.group.changed"):
            data = stored.event.data if isinstance(stored.event.data, dict) else {}
            found[str(data.get("group"))] = frozenset(cast(list[str], data.get("members") or []))
        return {name: members for name, members in sorted(found.items()) if members}

    async def set_group(self, by: Identity, group: str, members: Collection[str]) -> None:
        """Set who is in a group; an approval rule can then name ``group:<name>``."""
        changed = GroupChanged(group=group, members=sorted(set(members)))
        await self._security_event(by, "security.group.changed", changed)

    # ------------------------------------------------------------------ feedback (FR-HUM-09)

    async def give_feedback(
        self, by: Identity, app: str, run_id: str, given: FeedbackGiven
    ) -> Feedback:
        """Record what a person thought of a run, or one step of it. With ``remember``, the
        comment and correction are kept in the App's memory too, where its agents can find them."""
        state = await self.run_state(app, run_id, tenant=by.tenant)
        if state.started_event is None:
            raise NotFoundError(f"no run {run_id} of {app}")
        if given.step is not None and given.step not in state.steps:
            raise ValueError(f"run {run_id} has no step {given.step!r}")
        decision = await self.authorize(by, PolicyAction.RUN_FEEDBACK, app=app)
        if not decision.allowed:
            raise PolicyDeniedError(f"run.feedback: {decision.reason or 'not allowed'}")
        event = state.started_event.derive(
            "human.feedback.given",
            given.model_copy(update={"by": by.principal}).model_dump(mode="json"),
            source=f"aios://{by.tenant}/system/feedback",
            principal=by.principal,
        )
        await self.bus.publish(event)
        remembered = None
        if given.remember and (given.comment or given.correction is not None):
            remembered = await self._remember_feedback(by, event, given)
        return Feedback(event, remembered)

    async def _remember_feedback(self, by: Identity, event: Event, given: FeedbackGiven) -> str:
        decision = await self.policy.decide(
            PolicyRequest(
                tenant=by.tenant,
                app=event.appid,
                actor=by.actor,
                action=PolicyAction.MEMORY_WRITE,
                resource=Resource.memory_scope("app"),
            )
        )
        if not decision.allowed:
            payload = PolicyDecided(
                action=PolicyAction.MEMORY_WRITE.value, resource="app", reason=decision.reason
            )
            await self._security_event(by, "policy.decision.denied", payload)
            raise PolicyDeniedError(
                f"memory.write on app memory: {decision.reason or 'not allowed'}"
            )
        about = f"step {given.step}" if given.step else "a run"
        lines = [f"Feedback from {by.principal} on {about} ({given.rating or 'unrated'})."]
        if given.comment:
            lines.append(given.comment)
        if given.correction is not None:
            lines.append(f"A right result would have been: {json.dumps(given.correction)}")
        item = await self.memory.remember_as(
            by,
            "\n".join(lines),
            event=event,
            metadata={"feedback": event.id, "run": event.runid, "rating": given.rating},
        )
        return item.id

    async def feedback(self, tenant: str, app: str, *, run_id: str | None = None) -> list[Feedback]:
        """What people said about an App's runs, oldest first."""
        scope = Scope(tenant, app)
        found = self.bus.replay(scope, "human.feedback.given", run_id=run_id)
        return [Feedback(stored.event) async for stored in found]

    async def feedback_cases(self, tenant: str, app: str) -> list[EvalCase]:
        """Evaluation cases from corrections of whole runs: what the run was asked, and what
        the person said a right answer is. A step's correction isn't one: a case asks the App."""
        cases: list[EvalCase] = []
        for given in await self.feedback(tenant, app):
            if given.payload.step is not None or given.payload.correction is None:
                continue
            state = await self.run_state(app, str(given.event.runid), tenant=tenant)
            if state.input is None:
                continue
            cases.append(
                EvalCase(
                    id=f"feedback-{given.event.id.lower()}",
                    input=state.input,
                    expected=given.payload.correction,
                    tags=["feedback"],
                )
            )
        return cases

    def humans(self, tenant: str, app: str, capability: str) -> HumanCapability | None:
        """Who does ``capability`` in this App, if people do (FR-HUM-08)."""
        installed = self.apps.get((tenant, app))
        if installed is None:
            return None
        return next((h for h in installed.manifest.spec.humans if h.capability == capability), None)

    async def complete_task(
        self,
        by: Identity,
        task_id: str,
        output: JsonValue,
        *,
        comment: str | None = None,
        anyone: bool = False,
    ) -> HumanTask:
        """A person's work, as the result of the task it was. The kill switch applies."""
        task = await self.human.task(by.tenant, task_id)
        if task is None:
            raise NotFoundError(f"no task {task_id}")
        decision = await self.authorize(by, PolicyAction.TASK_WORK, app=task.app)
        if not decision.allowed:
            raise PolicyDeniedError(f"task.work: {decision.reason or 'not allowed'}")
        return await self.human.complete(by, task_id, output, comment=comment, anyone=anyone)

    async def contacts(self, tenant: str) -> dict[str, Contact]:
        """Where each person of a tenant is told about approvals, as they last set it."""
        found: dict[str, Contact] = {}
        scope = Scope(tenant, PLATFORM_APP_ID)
        async for stored in self.bus.replay(scope, "security.contact.changed"):
            data = ContactChanged.model_validate(stored.event.data)
            if stored.event.principal != data.principal:
                continue  # only a person sets their own: anything else isn't theirs
            if data.email is None and data.slack is None:
                found.pop(data.principal, None)
            else:
                found[data.principal] = Contact(email=data.email, slack=data.slack)
        return found

    async def set_contact(self, by: Identity, contact: Contact) -> None:
        """Set where ``by`` is told. Nobody can set it for anyone else."""
        changed = ContactChanged(principal=by.principal, email=contact.email, slack=contact.slack)
        await self._security_event(
            by, "security.contact.changed", changed, dataclass=DataClass.CONFIDENTIAL
        )

    def action_link(self, token: str) -> ActionLink:
        """The signed link ``token`` is (FR-HUM-07). Raises if it isn't one, or has expired."""
        link = self.links.read(token) if self.links is not None else None
        if link is None:
            raise AuthenticationError("this link isn't valid, or has expired")
        return link

    async def act(self, token: str, *, comment: str | None = None) -> Approval:
        """Answer as a signed link says, as its person, with the roles they have now."""
        link = self.action_link(token)
        roles = await self.security.roles(link.tenant, link.principal)
        person = Identity(link.principal, link.tenant, roles)
        return await self.decide_approval(
            person, link.approval, approve=link.approve, comment=comment, via=link.channel
        )

    async def _security_event(
        self,
        by: Identity,
        type: str,
        data: BaseModel,
        *,
        dataclass: DataClass = DataClass.INTERNAL,
    ) -> None:
        await self.bus.publish(
            Event(
                source=f"aios://{by.tenant}/system/security",
                type=type,
                tenantid=by.tenant,
                appid=PLATFORM_APP_ID,
                principal=by.principal,
                data=data.model_dump(mode="json"),
                dataclass=dataclass,
            )
        )

    async def fetch_document(self, url: str, *, tenant: str, app: str) -> Document:
        """A web page to ingest, through ``http.page.fetch`` and so through its domain allowlist.

        The server fetches this URL, so without the allowlist anyone allowed to ingest could make
        it read internal addresses.
        """
        provider = next((p for p in self._tool_providers if p.name == "http"), None)
        if provider is None:
            raise ToolNotFoundError("this installation has no http tool provider")
        egress = self.tools.egress(tenant, app, "http.page.fetch")
        page: Any = await provider.call(
            "http.page.fetch", {"url": url}, idempotency_key=None, egress=egress
        )
        return Document(url, str(page["text"]))

    async def create_key(
        self,
        by: Identity,
        name: str,
        roles: Collection[str],
        *,
        expires_in: timedelta | None = None,
    ) -> tuple[ApiKey, str]:
        """A new API key in the caller's tenant, and its value, which is shown once."""
        key, value = new_key(by.tenant, name, roles, created_by=by.principal, expires_in=expires_in)
        await self.security.add_key(key)
        changed = KeyChanged(key=key.id, name=key.name, roles=sorted(key.roles))
        await self._security_event(by, "security.key.created", changed)
        return key, value

    async def revoke_key(self, by: Identity, key_id: str) -> bool:
        keys = {key.id: key for key in await self.security.keys(by.tenant)}
        revoked = await self.security.revoke_key(by.tenant, key_id, datetime.now(UTC))
        if revoked:
            key = keys[key_id]
            # A run this key started loses its delegate's roles too. Another live key with this
            # name gets them back the next time it is used.
            await self.security.set_roles(by.tenant, key.principal, frozenset())
            changed = KeyChanged(key=key.id, name=key.name, roles=sorted(key.roles))
            await self._security_event(by, "security.key.revoked", changed)
        return revoked

    async def suspend(self, by: Identity, kind: Suspendable, name: str, *, reason: str) -> None:
        """The kill switch (FR-SEC-13): enforcement points refuse it from the next decision on."""
        await self.security.suspend(
            Suspension(by.tenant, kind, name, reason, by.principal, datetime.now(UTC))
        )
        changed = SuspensionChanged(kind=kind.value, name=name, reason=reason)
        await self._security_event(by, "security.suspended", changed)

    async def lift_suspension(self, by: Identity, kind: Suspendable, name: str) -> bool:
        lifted = await self.security.lift(by.tenant, kind, name)
        if lifted:
            changed = SuspensionChanged(kind=kind.value, name=name)
            await self._security_event(by, "security.resumed", changed)
        return lifted

    async def events(self, app: str, run_id: str, *, tenant: str | None = None) -> list[Event]:
        scope = Scope(tenant or self.tenant, app)
        return [stored.event async for stored in self.bus.replay(scope, run_id=run_id)]

    async def runs(
        self, app: str, *, tenant: str | None = None, limit: int = 50
    ) -> list[RunSummary]:
        """An App's runs, newest first, from the end of the log. Finished ones stay listed."""
        found: dict[str, RunSummary] = {}
        started = 0
        scope = Scope(tenant or self.tenant, app)
        async for stored in self.bus.latest(scope, "run.>"):
            event = stored.event
            status = _RUN_STATUS.get(event.type)
            if status is None or event.runid is None:
                continue
            run = found.setdefault(
                event.runid,
                RunSummary(run_id=event.runid, app=app, status=status, updated_at=event.time),
            )
            data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
            usage = data.get("usage")
            if isinstance(usage, dict) and not run.cost_usd:
                run.cost_usd = float(cast(dict[str, Any], usage).get("cost_usd", 0.0))
            run.tainted = run.tainted or event.tainted
            if event.type == "run.started":
                run.workflow = str(data.get("workflow"))
                run.started_at = event.time
                run.on_behalf_of = event.onbehalfof
                source = data.get("source")
                if isinstance(source, dict):
                    run.source_run = str(cast(dict[str, Any], source).get("run"))
                    run.mode = str(cast(dict[str, Any], source).get("mode"))
                started += 1
                if started >= limit:
                    break
        return [run for run in found.values() if run.started_at is not None]

    async def run_state(self, app: str, run_id: str, *, tenant: str | None = None) -> RunState:
        """A run's current state, folded from its events (FR-WF-08 queries)."""
        scope = Scope(tenant or self.tenant, app)
        return fold(run_id, [stored async for stored in self.bus.replay(scope, run_id=run_id)])

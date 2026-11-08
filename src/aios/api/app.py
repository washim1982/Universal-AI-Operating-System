"""The AIOS HTTP API (WP 1.7; FR-CP-01, 02, FR-APP-02).

* ``POST /v1/apply`` installs manifests (YAML); ``GET /v1/apps`` lists Apps.
* ``POST /v1/apps/{app}/runs`` starts a run through the chat entrypoint; ``GET`` one run's
  status, steps, output and cost; ``POST .../cancel``, ``.../pause``, ``.../resume`` and
  ``.../signal``; ``GET .../events`` as JSON or a live SSE stream (``Accept: text/event-stream``;
  resumable with ``Last-Event-ID``).
* ``GET /v1/events`` queries the log; ``/v1/dlq`` inspects, redrives and discards dead letters.
* ``/v1/audit`` reads and verifies the audit chain, ``/v1/keys`` manages API keys and
  ``/v1/suspensions`` is the kill switch (WP 2.2).
* ``/v1/memory`` searches, reads and erases memory, and ``/v1/apps/{app}/knowledge`` ingests
  documents (WP 2.1). The server never reads a file path: documents arrive in the request, and a
  URL goes through the ``http.page.fetch`` allowlist.
* ``/v1/approvals`` lists what the caller can decide, shows one approval and answers it (WP 2.4).
  Anyone else gets a 404, unless they may read the audit log.
* ``GET /metrics`` (Prometheus) and ``GET /healthz``, which says if answers are live or recorded.
* ``GET /demo``: a page an App ships, when the server was given one.
* ``/openai/v1``: each App as an OpenAI model, for OpenAI's clients (WP 3.7; ``openai_compat``).
* ``/mcp``: the Apps as tools of an MCP server, for MCP clients (WP 3.7; ``mcp_server``).

Every route but ``/healthz`` and ``/demo`` names the action it needs, and a policy decides
(FR-SEC-04). With an ``authenticator``, the tenant comes from the caller's credential, and a
request for any other tenant is refused (ADR-011). Without one, the server is a local development
server: the caller is anonymous, may do anything, and picks the tenant with ``X-AIOS-Tenant``.
Only a loopback address may run that way, which ``aios`` enforces when it starts a server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import urllib.parse
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, JsonValue, ValidationError, model_validator
from starlette.routing import Route

from aios import __version__
from aios.api import pages
from aios.api.mcp_server import MCPEndpoint
from aios.api.openai_compat import openai_routes
from aios.controlplane import ANONYMOUS, RunSummary, Runtime, views
from aios.controlplane.bundle import bundle_from_documents
from aios.controlplane.replay import KEEP
from aios.core.apps import Bundle, DataChoice, Grants, Installation
from aios.core.catalog import FeedbackGiven
from aios.core.envelope import DataClass, Event, Principal
from aios.core.errors import (
    AIOSError,
    ApprovalClosedError,
    ApprovalForbiddenError,
    AuthenticationError,
    ConflictError,
    InvalidTopicError,
    MemoryConflictError,
    NotFoundError,
    PolicyDeniedError,
    ToolDeniedError,
    ToolError,
    ToolInputError,
)
from aios.core.evals import Experiment, check_gate
from aios.core.evals import compare as compare_experiments_of
from aios.core.human import Approval, ApprovalStatus
from aios.core.identity import Authenticator, Identity, Suspendable
from aios.core.ids import Ulid
from aios.core.manifests import EvaluationManifest
from aios.core.memory import MemoryScope
from aios.core.policy import PolicyAction
from aios.core.schemas import envelope_schema, event_schema, manifest_schema, registry
from aios.core.topics import PLATFORM_APP_ID, SignalName, is_dns_label
from aios.fabric.port import Scope
from aios.human import Contact
from aios.memory import Document
from aios.observability import EventMetrics
from aios.observability.compare import Comparison
from aios.observability.trace import Trace, source_of
from aios.security.identity import describe
from aios.workflow import RunState

TERMINAL = ("run.completed", "run.failed", "run.cancelled")
PLATFORM_ROLE = "platform-admin"
LOCAL_ROLES = frozenset({PLATFORM_ROLE})
_POLL_SECONDS = 0.1
ANSWER_WAIT_SECONDS = 120.0


IngestScope = Literal["app", "tenant", "global"]


class RunRequest(BaseModel):
    message: str | None = None
    input: JsonValue = None
    data_class: DataClass = DataClass.INTERNAL
    session: Ulid | None = Field(
        default=None, description="Runs of one conversation share their session memory."
    )


class SignalRequest(BaseModel):
    name: SignalName
    data: JsonValue = None


class KeyRequest(BaseModel):
    name: str
    roles: list[str] = Field(min_length=1)
    expires_in_days: int | None = Field(default=None, ge=1)


class SuspendRequest(BaseModel):
    kind: Suspendable
    name: str
    reason: str = Field(min_length=1, max_length=500)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1)
    scopes: list[MemoryScope] = Field(
        default_factory=lambda: list[MemoryScope](("app", "tenant", "user")), min_length=1
    )
    k: int = Field(default=10, ge=1, le=100)
    filters: dict[str, JsonValue] | None = None


class ForkRequest(BaseModel):
    step: str | None = Field(
        default=None, description="Where to start again. For a retry, the step that failed"
    )
    input: JsonValue = Field(
        default=None, description="A new input for the run; left out, the same"
    )
    step_input: JsonValue = Field(default=None, description="A new input for that step")
    model: str | None = Field(default=None, description="A registry entry every model call uses")


GROUP_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
ACT_MAX_BYTES = 8192


class ExperimentRequest(BaseModel):
    suite: dict[str, JsonValue] = Field(description="An Evaluation manifest, as JSON")
    label: str | None = Field(default=None, max_length=200, description="What it tries")


class FeedbackRequest(BaseModel):
    step: str | None = Field(default=None, max_length=200)
    rating: Literal["good", "bad"] | None = None
    correction: JsonValue = Field(default=None, description="What a right result would have been")
    comment: str | None = Field(default=None, max_length=2000)
    remember: bool = Field(default=False, description="Keep it in the App's memory as well")


class TaskAnswer(BaseModel):
    output: JsonValue = Field(default=None, description="The work's result, as the run gets it")
    comment: str | None = Field(default=None, max_length=2000)


class ContactRequest(BaseModel):
    email: str | None = Field(default=None, max_length=320)
    slack: str | None = Field(default=None, max_length=24, description="A Slack member id")


class GroupRequest(BaseModel):
    members: list[str] = Field(
        default_factory=list[str], max_length=1000, description="Principals; none removes it"
    )


class GradeRequest(BaseModel):
    case: str
    grader: str
    score: float = Field(ge=0, le=1)
    comment: str | None = Field(default=None, max_length=2000)


class MemoryEdit(BaseModel):
    content: str = Field(min_length=1)
    version: int = Field(ge=1, description="The version being corrected, so two people can't clash")


Month = Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


class ForgetRequest(BaseModel):
    item: str | None = None
    subject: Principal | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.item is None) == (self.subject is None):
            raise ValueError("forget needs exactly one of item or subject")
        return self


class ApprovalAnswer(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)
    edits: dict[str, JsonValue] | None = Field(
        default=None, description="Approve: new values for some of the action's parameters"
    )


class KnowledgeDocument(BaseModel):
    uri: str = Field(min_length=1, max_length=2048)
    text: str


class KnowledgeRequest(BaseModel):
    source: str = Field(min_length=1, max_length=2048)
    documents: list[KnowledgeDocument]
    scope: IngestScope = "app"
    sensitivity: DataClass = DataClass.INTERNAL
    prune: bool = True


class KnowledgeUrlRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    scope: IngestScope = "app"
    sensitivity: DataClass = DataClass.INTERNAL


class RunStarted(BaseModel):
    run_id: str
    app: str
    events: str


class RunView(BaseModel):
    run_id: str
    app: str
    status: str
    waiting: list[str] = []
    tainted: bool = False
    taint: list[str] = []
    steps: dict[str, str] = {}
    output: JsonValue = None
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    events: int
    source: dict[str, JsonValue] | None = Field(
        default=None, description="The run this one replays, forks or retries"
    )


WEBHOOK_MAX_BYTES = 1_000_000


class InstallRequest(BaseModel):
    bundle: Bundle
    config: dict[str, Any] | None = None


class ConsentRequest(BaseModel):
    grants: Grants | None = None


class RollbackRequest(BaseModel):
    version: str


class Me(BaseModel):
    principal: str
    tenant: str
    roles: list[str] = []


def _source(state: RunState) -> dict[str, JsonValue] | None:
    copied = [step for step, found in state.steps.items() if found.copied]
    return source_of(state.source, copied)


def _event_json(event: Event) -> dict[str, Any]:
    return event.model_dump(mode="json", exclude_none=True)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


@dataclass(frozen=True, slots=True)
class Gate:
    """What every request passes: who is calling, and whether a policy allows it."""

    runtime: Runtime
    authenticator: Authenticator | None

    async def identify(self, authorization: str | None, tenant: str | None) -> Identity:
        if self.authenticator is None:
            chosen = tenant or self.runtime.tenant
            if not is_dns_label(chosen):  # "*" would mean every tenant to the fabric
                raise HTTPException(400, f"invalid tenant id: {chosen!r}")
            return Identity(ANONYMOUS, chosen, LOCAL_ROLES)
        scheme, _, credential = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not credential:
            raise _unauthorized("sign in: send Authorization: Bearer <API key or token>")
        try:
            identity = await self.authenticator.authenticate(credential.strip())
        except AuthenticationError as exc:
            raise _unauthorized("invalid credentials") from exc
        if tenant is not None and tenant != identity.tenant:
            raise HTTPException(403, "this credential belongs to another tenant")
        return identity


TenantHeader = Annotated[str | None, Header(alias="X-AIOS-Tenant")]
AuthorizationHeader = Annotated[str | None, Header()]


def requires(action: PolicyAction, *, per_app: bool = False) -> Callable[..., Awaitable[Identity]]:
    """A dependency: the caller, once a policy has allowed them ``action``."""

    async def caller(
        request: Request,
        authorization: AuthorizationHeader = None,
        x_aios_tenant: TenantHeader = None,
    ) -> Identity:
        gate: Gate = request.app.state.gate
        identity = await gate.identify(authorization, x_aios_tenant)
        app = request.path_params.get("app", PLATFORM_APP_ID) if per_app else PLATFORM_APP_ID
        decision = await gate.runtime.authorize(identity, action, app=app)
        if not decision.allowed:
            raise HTTPException(403, decision.reason or f"{action.value} is not allowed")
        return identity

    return caller


Installer = Annotated[Identity, Depends(requires(PolicyAction.APP_INSTALL))]
AppReader = Annotated[Identity, Depends(requires(PolicyAction.APP_READ))]
RunStarter = Annotated[Identity, Depends(requires(PolicyAction.RUN_START, per_app=True))]
RunReader = Annotated[Identity, Depends(requires(PolicyAction.RUN_READ, per_app=True))]
RunOperator = Annotated[Identity, Depends(requires(PolicyAction.RUN_CONTROL, per_app=True))]
EventReader = Annotated[Identity, Depends(requires(PolicyAction.EVENT_READ))]
DlqManager = Annotated[Identity, Depends(requires(PolicyAction.DLQ_MANAGE))]
AuditReader = Annotated[Identity, Depends(requires(PolicyAction.AUDIT_READ))]
KeyManager = Annotated[Identity, Depends(requires(PolicyAction.KEY_MANAGE))]
MemoryManager = Annotated[Identity, Depends(requires(PolicyAction.MEMORY_MANAGE))]
AppManager = Annotated[Identity, Depends(requires(PolicyAction.APP_MANAGE))]
ApprovalReader = Annotated[Identity, Depends(requires(PolicyAction.APPROVAL_READ))]
ApprovalDecider = Annotated[Identity, Depends(requires(PolicyAction.APPROVAL_DECIDE))]
TaskWorker = Annotated[Identity, Depends(requires(PolicyAction.TASK_WORK))]
FeedbackGiver = Annotated[Identity, Depends(requires(PolicyAction.RUN_FEEDBACK, per_app=True))]
Suspender = Annotated[Identity, Depends(requires(PolicyAction.SUSPEND))]
MetricsReader = Annotated[Identity, Depends(requires(PolicyAction.METRICS_READ))]
EvalRunner = Annotated[Identity, Depends(requires(PolicyAction.EVAL_RUN, per_app=True))]
EvalReader = Annotated[Identity, Depends(requires(PolicyAction.EVAL_READ, per_app=True))]
EvalGrader = Annotated[Identity, Depends(requires(PolicyAction.EVAL_GRADE, per_app=True))]


def create_app(  # noqa: PLR0915 - the route table
    runtime: Runtime,
    *,
    metrics: EventMetrics | None = None,
    demo: Path | None = None,
    authenticator: Authenticator | None = None,
    answer_wait: float = ANSWER_WAIT_SECONDS,
) -> FastAPI:
    """The API. ``answer_wait`` is how long a client that wants an answer back (OpenAI's chat
    completions, MCP) is kept waiting before it is told the run is still working."""
    gate = Gate(runtime, authenticator)
    mcp = MCPEndpoint(runtime, gate.identify, wait_seconds=answer_wait, local=authenticator is None)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        async with mcp.running():
            yield

    api = FastAPI(title="AIOS API", version=__version__, lifespan=lifespan)
    api.state.gate = gate
    api.include_router(openai_routes(runtime, wait_seconds=answer_wait))
    api.router.routes.append(Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]))

    def installed(app: str, tenant: str) -> None:
        try:
            runtime.app(app, tenant=tenant)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    async def state_of(app: str, run_id: str, tenant: str) -> RunState:
        installed(app, tenant)
        state = await runtime.run_state(app, run_id, tenant=tenant)
        if state.events == 0:
            raise HTTPException(404, f"no run {run_id}")
        return state

    async def unfinished(app: str, run_id: str, tenant: str) -> RunState:
        state = await state_of(app, run_id, tenant)
        if state.finished:
            raise HTTPException(409, f"run {run_id} is already {state.status}")
        return state

    @api.get("/healthz")
    async def health() -> dict[str, str]:
        replaying = runtime.cassette is not None and runtime.cassette.replaying
        answers = "recorded" if replaying else "live"
        return {"status": "ok", "version": __version__, "answers": answers}

    if demo is not None:

        @api.get("/demo", include_in_schema=False)
        async def demo_page() -> FileResponse:
            return FileResponse(demo)

    @api.get("/metrics")
    async def prometheus(_caller: MetricsReader) -> Response:
        if metrics is None:
            raise HTTPException(404, "metrics are not enabled")
        return Response(metrics.exposition(), media_type="text/plain; version=0.0.4")

    def install_failure(exc: Exception) -> HTTPException:
        if isinstance(exc, NotFoundError):
            return HTTPException(404, str(exc))
        if isinstance(exc, ConflictError):
            return HTTPException(409, str(exc))
        return HTTPException(422, str(exc))

    async def view(installation: Installation) -> dict[str, Any]:
        shown: dict[str, Any] = installation.model_dump(mode="json")
        if installation.pending is not None:
            wanted = await runtime.installer.requested(
                installation.tenant, installation.app, installation.pending
            )
            shown["requested"] = wanted.model_dump(mode="json")
        return shown

    async def applied(caller: Identity, installation: Installation) -> dict[str, Any]:
        """Bring this process up to date before answering, so that the App is there on return."""
        await runtime.reconciler.sync(caller.tenant, installation.app)
        return await view(installation)

    @api.post("/v1/apply", status_code=201)
    async def apply(
        document: Annotated[str, Body(media_type="application/yaml")],
        caller: Installer,
        consent: Literal["ask", "all"] = "ask",
    ) -> dict[str, Any]:
        """Offer manifests as a version. ``consent=all`` agrees to all it asks for, in one step."""
        try:
            bundle = bundle_from_documents(document)
            found = await runtime.installer.propose(caller, bundle)
            if consent == "all" and found.pending is not None:
                found = await runtime.installer.consent(caller, bundle.name)
            return await applied(caller, found)
        except (AIOSError, ValueError, ImportError, yaml.YAMLError) as exc:
            raise install_failure(exc) from exc

    @api.get("/v1/installations")
    async def installations(caller: AppManager) -> list[dict[str, Any]]:
        return [await view(i) for i in await runtime.installations.find(caller.tenant)]

    @api.get("/v1/installations/{app}")
    async def installation(app: str, caller: AppManager) -> dict[str, Any]:
        found = await runtime.installations.get(caller.tenant, app)
        if found is None:
            raise HTTPException(404, f"App {app!r} isn't installed")
        return await view(found)

    @api.get("/v1/installations/{app}/versions")
    async def versions(app: str, caller: AppManager) -> list[dict[str, Any]]:
        """Every version this tenant has had, newest first, and what each asks for."""
        try:
            current = await runtime.installations.get(caller.tenant, app)
            if current is None:
                raise NotFoundError(f"{app!r} isn't installed here")
            found: list[dict[str, Any]] = []
            for bundle in reversed(await runtime.installations.versions(caller.tenant, app)):
                wanted = await runtime.installer.requested(caller.tenant, app, bundle.version)
                found.append(
                    {
                        "version": bundle.version,
                        "digest": bundle.digest,
                        "running": bundle.version == current.version,
                        "pending": bundle.version == current.pending,
                        "requested": wanted.model_dump(mode="json"),
                    }
                )
        except (AIOSError, ValueError) as exc:
            raise install_failure(exc) from exc
        return found

    @api.post("/v1/installations", status_code=201)
    async def install_app(request: InstallRequest, caller: Installer) -> dict[str, Any]:
        """Offer a version. It waits for consent unless it asks for nothing new."""
        try:
            found = await runtime.installer.propose(caller, request.bundle, config=request.config)
            return await applied(caller, found)
        except (AIOSError, ValueError, ImportError) as exc:
            raise install_failure(exc) from exc

    @api.post("/v1/installations/{app}/consent")
    async def consent_to(
        app: str, caller: AppManager, request: ConsentRequest | None = None
    ) -> dict[str, Any]:
        grants = request.grants if request is not None else None
        try:
            found = await runtime.installer.consent(caller, app, grants=grants)
            return await applied(caller, found)
        except (AIOSError, ValueError, ImportError) as exc:
            raise install_failure(exc) from exc

    @api.post("/v1/installations/{app}/refuse")
    async def refuse(app: str, caller: AppManager) -> dict[str, Any]:
        try:
            return await applied(caller, await runtime.installer.refuse(caller, app))
        except AIOSError as exc:
            raise install_failure(exc) from exc

    @api.post("/v1/installations/{app}/rollback")
    async def rollback(app: str, request: RollbackRequest, caller: AppManager) -> dict[str, Any]:
        try:
            found = await runtime.installer.rollback(caller, app, request.version)
            return await applied(caller, found)
        except (AIOSError, ValueError, ImportError) as exc:
            raise install_failure(exc) from exc

    @api.put("/v1/installations/{app}/config")
    async def configure(app: str, config: dict[str, Any], caller: AppManager) -> dict[str, Any]:
        try:
            return await applied(caller, await runtime.installer.configure(caller, app, config))
        except (AIOSError, ValueError, ImportError) as exc:
            raise install_failure(exc) from exc

    @api.get("/v1/installations/{app}/export")
    async def export_app(app: str, caller: AppManager) -> StreamingResponse:
        """Everything an App has: its events, then its memory, as one JSON object a line."""
        known = await runtime.installations.get(caller.tenant, app)
        if known is None and (caller.tenant, app) not in runtime.apps:
            raise HTTPException(404, f"no App {app!r} here")

        async def lines() -> AsyncIterator[str]:
            async for stored in runtime.bus.replay(Scope(caller.tenant, app)):
                yield json.dumps({"event": _event_json(stored.event)}) + "\n"
            for item in await runtime.memory.of_app(caller.tenant, app):
                yield json.dumps({"memory": item.model_dump(mode="json")}) + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @api.delete("/v1/installations/{app}")
    async def uninstall(app: str, caller: AppManager, data: DataChoice) -> dict[str, Any]:
        """``data`` has no default: what becomes of it is a decision somebody has to make."""
        try:
            return await view(await runtime.uninstall(caller, app, data))
        except AIOSError as exc:
            raise install_failure(exc) from exc

    @api.get("/v1/apps")
    async def apps(caller: AppReader) -> list[dict[str, Any]]:
        return [
            {
                "app": app.name,
                "version": app.manifest.metadata.version,
                "agents": [a.name for a in app.agents],
                "workflows": sorted(app.workflows),
            }
            for (owner, _), app in runtime.apps.items()
            if owner == caller.tenant
        ]

    # ------------------------------------------------------------------ schema registry

    @api.get("/v1/schemas")
    async def schemas(caller: AppReader) -> dict[str, Any]:
        """Every event type and manifest kind, and where its JSON Schema is (FR-EVT-10)."""
        return registry()

    @api.get("/v1/schemas/envelope")
    async def schema_of_envelope(caller: AppReader) -> dict[str, Any]:
        return envelope_schema()

    @api.get("/v1/schemas/events/{event_type}")
    async def schema_of_event(event_type: str, caller: AppReader) -> dict[str, Any]:
        found = event_schema(event_type)
        if found is None:
            raise HTTPException(404, f"no schema for {event_type}")
        return found

    @api.get("/v1/schemas/manifests/{kind}")
    async def schema_of_manifest(kind: str, caller: AppReader) -> dict[str, Any]:
        found = manifest_schema(kind)
        if found is None:
            raise HTTPException(404, f"no manifest kind {kind}")
        return found

    @api.get("/v1/apps/{app}")
    async def describe_app(app: str, caller: AppReader) -> dict[str, Any]:
        """What the App is made of: its agents, the work people do, its workflows and steps."""
        installed(app, caller.tenant)
        return views.app_of(runtime.app(app, tenant=caller.tenant))

    @api.post("/v1/apps/{app}/runs", status_code=202)
    async def start_run(
        app: str,
        request: RunRequest,
        caller: RunStarter,
        traceparent: Annotated[str | None, Header()] = None,
    ) -> RunStarted:
        installed(app, caller.tenant)
        try:
            run_id = await runtime.chat(
                app,
                request.message,
                input=request.input,
                tenant=caller.tenant,
                principal=caller.principal,
                data_class=request.data_class,
                session=request.session,
                traceparent=traceparent,
            )
        except PolicyDeniedError as exc:
            raise HTTPException(403, str(exc)) from exc
        return RunStarted(run_id=run_id, app=app, events=f"/v1/apps/{app}/runs/{run_id}/events")

    async def restarted(
        app: str,
        run_id: str,
        mode: Literal["replay", "fork", "retry"],
        caller: Identity,
        request: ForkRequest | None = None,
    ) -> RunStarted:
        installed(app, caller.tenant)
        asked = request or ForkRequest()
        try:
            new = await runtime.restart(
                app,
                run_id,
                mode,
                tenant=caller.tenant,
                principal=caller.principal,
                step=asked.step,
                input=asked.input if "input" in asked.model_fields_set else KEEP,
                step_input=asked.step_input,
                model=asked.model,
            )
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except PolicyDeniedError as exc:
            raise HTTPException(403, str(exc)) from exc
        return RunStarted(run_id=new, app=app, events=f"/v1/apps/{app}/runs/{new}/events")

    @api.post("/v1/apps/{app}/runs/{run_id}/replay", status_code=202)
    async def replay_run(app: str, run_id: str, caller: RunStarter) -> RunStarted:
        """Run it again with every model and tool call answered from its record (FR-OBS-04)."""
        return await restarted(app, run_id, "replay", caller)

    @api.post("/v1/apps/{app}/runs/{run_id}/fork", status_code=202)
    async def fork_run(
        app: str, run_id: str, request: ForkRequest, caller: RunStarter
    ) -> RunStarted:
        """Start again from a step, with changes, and side effects held back (FR-OBS-04)."""
        if request.step is None:
            raise HTTPException(422, "say which step to fork from")
        return await restarted(app, run_id, "fork", caller, request)

    @api.post("/v1/apps/{app}/runs/{run_id}/retry", status_code=202)
    async def retry_run(
        app: str, run_id: str, caller: RunOperator, request: ForkRequest | None = None
    ) -> RunStarted:
        """Start again from a step for real: side effects happen again (FR-WF-11)."""
        return await restarted(app, run_id, "retry", caller, request)

    @api.get("/v1/apps/{app}/runs/{run_id}/compare/{other}")
    async def compare_runs(app: str, run_id: str, other: str, caller: RunReader) -> Comparison:
        """Two runs side by side, step by step: usually a run and its replay or fork."""
        await state_of(app, run_id, caller.tenant)
        await state_of(app, other, caller.tenant)
        return await views.comparison(runtime, caller.tenant, app, run_id, other)

    # ------------------------------------------------------------------ groups

    @api.get("/v1/groups")
    async def list_groups(caller: KeyManager) -> dict[str, list[str]]:
        """The tenant's groups and their members, which approvals name as ``group:<name>``."""
        return {
            name: sorted(members) for name, members in (await runtime.groups(caller.tenant)).items()
        }

    @api.put("/v1/groups/{group}")
    async def set_group(group: str, request: GroupRequest, caller: KeyManager) -> dict[str, Any]:
        """Set who is in a group. Members are principals of the caller's tenant."""
        if not GROUP_NAME.fullmatch(group):
            raise HTTPException(
                422, "a group's name is letters, digits, dots, dashes and underscores"
            )
        strangers = [m for m in request.members if not m.endswith(f"@{caller.tenant}")]
        if strangers:
            raise HTTPException(422, f"not of tenant {caller.tenant}: {', '.join(strangers)}")
        await runtime.set_group(caller, group, request.members)
        return {"group": group, "members": sorted(set(request.members))}

    # ------------------------------------------------------------------ evaluation

    def experiment_view(found: Experiment) -> dict[str, Any]:
        gate = check_gate(found, found.gate)
        return {**found.summary(), "gate_result": gate.model_dump(mode="json")}

    async def experiment_of_app(app: str, experiment: str, tenant: str) -> Experiment:
        installed(app, tenant)
        found = await runtime.evaluator.experiment(tenant, app, experiment)
        if found is None:
            raise HTTPException(404, f"no experiment {experiment}")
        return found

    @api.post("/v1/apps/{app}/experiments", status_code=202)
    async def run_experiment(
        app: str, request: ExperimentRequest, caller: EvalRunner
    ) -> dict[str, str]:
        """Run an evaluation suite against the App; a worker runs it (FR-OBS-07)."""
        installed(app, caller.tenant)
        try:
            suite = EvaluationManifest.model_validate(request.suite)
        except ValueError as exc:
            raise HTTPException(422, f"not an evaluation: {exc}") from exc
        try:
            experiment = await runtime.evaluator.request(
                caller.tenant,
                app,
                suite,
                principal=caller.principal,
                on_behalf_of=None if caller.principal == ANONYMOUS else caller.principal,
                label=request.label,
            )
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"experiment": experiment, "app": app}

    @api.get("/v1/apps/{app}/experiments")
    async def experiments(app: str, caller: EvalReader, limit: int = 50) -> list[dict[str, Any]]:
        """The App's experiments, newest first."""
        installed(app, caller.tenant)
        found = await runtime.evaluator.experiments(caller.tenant, app, min(max(limit, 1), 200))
        return [experiment_view(experiment) for experiment in found]

    @api.get("/v1/apps/{app}/experiments/{experiment}")
    async def experiment(app: str, experiment: str, caller: EvalReader) -> dict[str, Any]:
        """An experiment: each case, its grades, its score, and whether it passes its gate."""
        return experiment_view(await experiment_of_app(app, experiment, caller.tenant))

    @api.get("/v1/apps/{app}/experiments/{experiment}/compare/{other}")
    async def compare_experiments(
        app: str, experiment: str, other: str, caller: EvalReader
    ) -> dict[str, Any]:
        """Two experiments case by case: one suite on two versions of the App."""
        a = await experiment_of_app(app, experiment, caller.tenant)
        b = await experiment_of_app(app, other, caller.tenant)
        return compare_experiments_of(a, b).view()

    @api.post("/v1/apps/{app}/experiments/{experiment}/grades")
    async def grade_case(
        app: str, experiment: str, request: GradeRequest, caller: EvalGrader
    ) -> dict[str, Any]:
        """A person's grade for a case that asked for one."""
        installed(app, caller.tenant)
        try:
            found = await runtime.evaluator.grade(
                caller,
                app,
                experiment,
                case=request.case,
                grader=request.grader,
                score=request.score,
                comment=request.comment,
            )
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PolicyDeniedError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return experiment_view(found)

    @api.get("/v1/apps/{app}/evaluations/online")
    async def online_evaluations(
        app: str, caller: EvalReader, limit: int = 50
    ) -> list[dict[str, JsonValue]]:
        """Production runs graded as they finished (FR-OBS-07 online evaluation)."""
        installed(app, caller.tenant)
        return await runtime.evaluator.online(caller.tenant, app, min(max(limit, 1), 200))

    @api.post("/v1/hooks/{tenant}/{app}/{name}", status_code=202)
    async def webhook(
        tenant: str,
        app: str,
        name: str,
        request: Request,
        signature: Annotated[str | None, Header(alias="X-AIOS-Signature")] = None,
    ) -> RunStarted:
        """An outside system starts a run. Its signature stands in for a credential."""
        body = await request.body()
        if len(body) > WEBHOOK_MAX_BYTES:
            raise HTTPException(413, "that request is too large")
        try:
            run_id = await runtime.receive_webhook(tenant, app, name, body, signature)
        except AuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return RunStarted(run_id=run_id, app=app, events=f"/v1/apps/{app}/runs/{run_id}/events")

    @api.get("/v1/agents")
    async def agents(caller: AppReader) -> list[views.AgentView]:
        """Every agent, its version, what it may do, whether it is stopped, and its recent work."""
        return await views.agents_of(runtime, caller.tenant)

    @api.get("/v1/apps/{app}/policies")
    async def app_policies(app: str, caller: AuditReader) -> views.PolicyView:
        """An App's policies as written and as Cedar runs them (FR-UI-06)."""
        try:
            return views.policies_of(runtime, caller.tenant, app)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/v1/apps/{app}/policies/simulate")
    async def simulate_policy(
        app: str, question: views.Simulation, caller: AuditReader
    ) -> views.Verdict:
        """Would this be allowed now? Nothing happens, and nothing is recorded."""
        try:
            return await views.simulate(runtime, caller.tenant, app, question)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/v1/me")
    async def me(caller: AppReader) -> Me:
        return Me(principal=caller.principal, tenant=caller.tenant, roles=sorted(caller.roles))

    @api.get("/v1/me/contact")
    async def my_contact(caller: AppReader) -> ContactRequest:
        """Where you are told about approvals for you (FR-HUM-07)."""
        found = (await runtime.contacts(caller.tenant)).get(caller.principal) or Contact()
        return ContactRequest(email=found.email, slack=found.slack)

    @api.put("/v1/me/contact")
    async def set_my_contact(request: ContactRequest, caller: AppReader) -> ContactRequest:
        """Set your own: nobody can set it for you, so nobody can send your links elsewhere."""
        try:
            contact = Contact(email=request.email or None, slack=request.slack or None)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        await runtime.set_contact(caller, contact)
        return ContactRequest(email=contact.email, slack=contact.slack)

    # ------------------------------------------------------------------ signed approval links

    @api.get("/v1/act/{token}", response_class=HTMLResponse, include_in_schema=False)
    async def action_page(token: str) -> HTMLResponse:
        """What a signed link asks, and a button. Opening it decides nothing."""
        try:
            link = runtime.action_link(token)
        except AuthenticationError as exc:
            return pages.message("This link can't be used", str(exc), status=401)
        approval = await runtime.human.get(link.tenant, link.approval)
        if approval is None:
            return pages.message("Nothing to answer", "That approval isn't there.", status=404)
        return pages.confirmation(link, approval)

    @api.post("/v1/act/{token}", response_class=HTMLResponse, include_in_schema=False)
    async def act(token: str, request: Request) -> HTMLResponse:
        """The answer a signed link carries, given by its person."""
        body = await request.body()
        if len(body) > ACT_MAX_BYTES:
            return pages.message("Too long", "That comment is too long.", status=413)
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"), max_num_fields=4)
        comment = (form.get("comment") or [""])[0].strip()[:2000] or None
        try:
            link = runtime.action_link(token)
            decided = await runtime.act(token, comment=comment)
        except AuthenticationError as exc:
            return pages.message("This link can't be used", str(exc), status=401)
        except NotFoundError:
            return pages.message("Nothing to answer", "That approval isn't there.", status=404)
        except ApprovalClosedError as exc:
            return pages.message("Already answered", str(exc), status=409)
        except (ApprovalForbiddenError, PolicyDeniedError) as exc:
            return pages.message("You can't answer this", str(exc), status=403)
        return pages.answered(link, decided)

    @api.get("/v1/apps/{app}/runs")
    async def list_runs(
        app: str, caller: RunReader, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[RunSummary]:
        installed(app, caller.tenant)
        return await runtime.runs(app, tenant=caller.tenant, limit=limit)

    @api.get("/v1/apps/{app}/runs/{run_id}")
    async def get_run(app: str, run_id: str, caller: RunReader) -> RunView:
        state = await state_of(app, run_id, caller.tenant)
        end = state.ended
        data: dict[str, Any] = end.data if end is not None and isinstance(end.data, dict) else {}
        return RunView(
            run_id=run_id,
            app=app,
            status="running" if state.status == "new" else state.status,
            waiting=sorted(state.waiting),
            tainted=state.tainted,
            taint=sorted(state.taint),
            steps={step.id: step.status for step in state.steps.values()},
            output=data.get("output"),
            usage=data.get("usage"),
            error=data.get("error"),
            events=state.events,
            source=_source(state),
        )

    @api.post("/v1/apps/{app}/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(app: str, run_id: str, caller: RunOperator) -> dict[str, str]:
        await state_of(app, run_id, caller.tenant)
        await runtime.cancel(app, run_id, tenant=caller.tenant, principal=caller.principal)
        return {"run_id": run_id, "status": "cancelling"}

    @api.post("/v1/apps/{app}/runs/{run_id}/pause", status_code=202)
    async def pause_run(app: str, run_id: str, caller: RunOperator) -> dict[str, str]:
        state = await unfinished(app, run_id, caller.tenant)
        if state.status == "new":  # the engine only acts on a command once the run has started
            raise HTTPException(409, f"run {run_id} has not started yet")
        await runtime.pause(app, run_id, tenant=caller.tenant, principal=caller.principal)
        return {"run_id": run_id, "status": "pausing"}

    @api.post("/v1/apps/{app}/runs/{run_id}/resume", status_code=202)
    async def resume_run(app: str, run_id: str, caller: RunOperator) -> dict[str, str]:
        state = await unfinished(app, run_id, caller.tenant)
        if not state.held:
            raise HTTPException(409, f"run {run_id} is not paused by request")
        await runtime.resume(app, run_id, tenant=caller.tenant, principal=caller.principal)
        return {"run_id": run_id, "status": "resuming"}

    @api.post("/v1/apps/{app}/runs/{run_id}/signal", status_code=202)
    async def signal_run(
        app: str, run_id: str, signal: SignalRequest, caller: RunOperator
    ) -> dict[str, str]:
        await unfinished(app, run_id, caller.tenant)
        await runtime.signal(
            app,
            run_id,
            signal.name,
            signal.data,
            tenant=caller.tenant,
            principal=caller.principal,
        )
        return {"run_id": run_id, "signal": signal.name, "status": "sent"}

    @api.get("/v1/apps/{app}/runs/{run_id}/trace")
    async def run_trace(app: str, run_id: str, caller: RunReader) -> Trace:
        """The run as a graph of its steps, with what each did (FR-OBS-03)."""
        installed(app, caller.tenant)
        try:
            return await views.trace(runtime, caller.tenant, app, run_id)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/v1/apps/{app}/runs/{run_id}/events", response_model=None)
    async def run_events(
        app: str,
        run_id: str,
        request: Request,
        caller: RunReader,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> Any:
        await state_of(app, run_id, caller.tenant)
        scope = Scope(caller.tenant, app)
        if "text/event-stream" not in request.headers.get("accept", ""):
            return [_event_json(s.event) async for s in runtime.bus.replay(scope, run_id=run_id)]
        after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
        return StreamingResponse(
            _stream(runtime, scope, run_id, after, request), media_type="text/event-stream"
        )

    @api.get("/v1/events")
    async def events(  # the parameters are the query, so keyword-only
        *,
        caller: EventReader,
        app: str | None = None,
        pattern: str = ">",
        after: int = 0,
        limit: Annotated[int, Query(ge=1, le=5000)] = 500,
        newest: bool = False,
    ) -> list[dict[str, Any]]:
        """Events as they were stored, from ``after``; or with ``newest``, the latest first."""
        try:
            scope = Scope(caller.tenant, app or "*")
            found: list[dict[str, Any]] = []
            reading = (
                runtime.bus.latest(scope, pattern)
                if newest
                else runtime.bus.replay(scope, pattern, after=after)
            )
            async for stored in reading:
                found.append({"position": stored.position, **_event_json(stored.event)})
                if len(found) >= limit:
                    break
        except (InvalidTopicError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return found

    @api.get("/v1/costs")
    async def cost_report(caller: EventReader, month: Month = None) -> views.CostReport:
        """A month's spending by App, agent, model and person, and its budgets (FR-OBS-06)."""
        return await views.costs(runtime, caller.tenant, month or f"{datetime.now(UTC):%Y-%m}")

    @api.get("/v1/dlq")
    async def dead_letters(caller: EventReader) -> list[dict[str, Any]]:
        letters = await runtime.bus.dead_letters(Scope(caller.tenant))
        return [
            {
                "id": letter.id,
                "group": letter.group,
                "event": _event_json(letter.stored.event),
                "attempts": letter.attempts,
                "reason": letter.reason,
                "dead_at": letter.dead_at.isoformat(),
            }
            for letter in letters
        ]

    @api.post("/v1/dlq/{letter}/redrive", status_code=202)
    async def redrive(letter: str, caller: DlqManager) -> dict[str, str]:
        try:
            await runtime.bus.redrive(Scope(caller.tenant), letter)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"id": letter, "status": "redriven"}

    @api.delete("/v1/dlq/{letter}", status_code=204)
    async def discard(letter: str, caller: DlqManager) -> Response:
        try:
            await runtime.bus.discard(Scope(caller.tenant), letter)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(status_code=204)

    @api.get("/v1/audit")
    async def audit_entries(
        caller: AuditReader, after: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        entries = await runtime.audit.entries(caller.tenant, after=after, limit=limit)
        return [
            {"seq": entry.seq, "hash": entry.hash, **_event_json(entry.event)} for entry in entries
        ]

    @api.get("/v1/audit/verify")
    async def audit_verify(caller: AuditReader) -> dict[str, Any]:
        return asdict(await runtime.audit.verify(caller.tenant))

    @api.get("/v1/keys")
    async def keys(caller: KeyManager) -> list[dict[str, Any]]:
        return [describe(key) for key in await runtime.security.keys(caller.tenant)]

    @api.post("/v1/keys", status_code=201)
    async def create_key(request: KeyRequest, caller: KeyManager) -> dict[str, Any]:
        if PLATFORM_ROLE in request.roles and PLATFORM_ROLE not in caller.roles:
            raise HTTPException(403, f"only a {PLATFORM_ROLE} can grant {PLATFORM_ROLE}")
        days = request.expires_in_days
        try:
            key, value = await runtime.create_key(
                caller,
                request.name,
                request.roles,
                expires_in=timedelta(days=days) if days else None,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**describe(key), "key": value}

    @api.delete("/v1/keys/{key_id}", status_code=204)
    async def revoke_key(key_id: str, caller: KeyManager) -> Response:
        if not await runtime.revoke_key(caller, key_id):
            raise HTTPException(404, f"no active key {key_id}")
        return Response(status_code=204)

    @api.post("/v1/memory/search")
    async def memory_search(
        request: MemorySearchRequest, caller: MemoryManager
    ) -> list[dict[str, Any]]:
        hits = await runtime.memory.search_as(
            caller, request.query, scopes=request.scopes, k=request.k, filters=request.filters
        )
        return [{"score": hit.score, **hit.item.model_dump(mode="json")} for hit in hits]

    @api.post("/v1/memory/forget")
    async def memory_forget(request: ForgetRequest, caller: MemoryManager) -> dict[str, int]:
        erasure = await runtime.memory.forget_as(caller, item=request.item, subject=request.subject)
        return erasure.model_dump()

    @api.get("/v1/memory/{item_id}")
    async def memory_item(item_id: str, caller: MemoryManager) -> dict[str, Any]:
        item = await runtime.memory.get_as(caller, item_id)
        if item is None:
            raise HTTPException(404, f"no memory item {item_id}")
        return item.model_dump(mode="json")

    @api.put("/v1/memory/{item_id}")
    async def memory_update(
        item_id: str, request: MemoryEdit, caller: MemoryManager
    ) -> dict[str, Any]:
        """Correct an item. It becomes a new version, and the change is audited (FR-UI-07)."""
        try:
            item = await runtime.memory.update_as(
                caller,
                item_id,
                content=request.content,
                version=request.version,
                global_too=PLATFORM_ROLE in caller.roles,
            )
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except MemoryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except PolicyDeniedError as exc:
            raise HTTPException(403, str(exc)) from exc
        return item.model_dump(mode="json")

    def ingestible(scope: IngestScope, caller: Identity) -> None:
        if scope == "global" and PLATFORM_ROLE not in caller.roles:  # every tenant reads it
            raise HTTPException(403, f"only a {PLATFORM_ROLE} can write global memory")

    @api.put("/v1/apps/{app}/knowledge")
    async def knowledge(
        app: str, request: KnowledgeRequest, caller: MemoryManager
    ) -> dict[str, Any]:
        installed(app, caller.tenant)
        ingestible(request.scope, caller)
        documents = [Document(d.uri, d.text) for d in request.documents]
        try:
            report = await runtime.memory.sync(
                caller,
                app,
                documents,
                source=request.source,
                scope=request.scope,
                sensitivity=request.sensitivity,
                prune=request.prune,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return asdict(report)

    @api.post("/v1/apps/{app}/knowledge/url")
    async def knowledge_url(
        app: str, request: KnowledgeUrlRequest, caller: MemoryManager
    ) -> dict[str, Any]:
        installed(app, caller.tenant)
        ingestible(request.scope, caller)
        try:
            document = await runtime.fetch_document(request.url, tenant=caller.tenant, app=app)
        except ToolDeniedError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ToolInputError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ToolError as exc:
            raise HTTPException(502, str(exc)) from exc
        report = await runtime.memory.sync(
            caller,
            app,
            [document],
            source=request.url,
            scope=request.scope,
            sensitivity=request.sensitivity,
            prune=False,
        )
        return asdict(report)

    async def sees_all_approvals(caller: Identity) -> bool:
        """An auditor sees every approval. Without sign-in there are no identities to go by."""
        if authenticator is None:
            return True
        return (await runtime.authorize(caller, PolicyAction.AUDIT_READ)).allowed

    # ------------------------------------------------------------------ feedback

    @api.post("/v1/apps/{app}/runs/{run_id}/feedback", status_code=201)
    async def give_feedback(
        app: str, run_id: str, request: FeedbackRequest, caller: FeedbackGiver
    ) -> dict[str, Any]:
        """Rate or correct what a run, or one of its steps, did (FR-HUM-09)."""
        installed(app, caller.tenant)
        try:
            given = FeedbackGiven(by=caller.principal, **request.model_dump())
            found = await runtime.give_feedback(caller, app, run_id, given)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()[0]["msg"]) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PolicyDeniedError as exc:
            raise HTTPException(403, str(exc)) from exc
        return found.view()

    @api.get("/v1/apps/{app}/feedback")
    async def feedback(app: str, caller: RunReader, run: str | None = None) -> list[dict[str, Any]]:
        installed(app, caller.tenant)
        return [f.view() for f in await runtime.feedback(caller.tenant, app, run_id=run)]

    @api.get("/v1/apps/{app}/feedback/cases")
    async def feedback_cases(app: str, caller: EvalReader) -> list[dict[str, Any]]:
        """Evaluation cases from corrections, to add to an Evaluation's ``spec.cases``."""
        installed(app, caller.tenant)
        cases = await runtime.feedback_cases(caller.tenant, app)
        return [case.model_dump(mode="json", exclude_defaults=True) for case in cases]

    # ------------------------------------------------------------------ work for people

    @api.get("/v1/tasks")
    async def tasks(caller: TaskWorker, everyone: bool = False) -> list[dict[str, Any]]:
        """Open work assigned to people that you can do (FR-HUM-08). ``everyone`` is for
        operators: all of the tenant's."""
        if authenticator is None:
            everyone = True
        elif everyone and not (await runtime.authorize(caller, PolicyAction.RUN_CONTROL)).allowed:
            raise HTTPException(403, "only an operator sees everyone's work")
        return [t.view() for t in await runtime.human.assigned(caller, everyone=everyone)]

    @api.get("/v1/tasks/{task_id}")
    async def task(task_id: str, caller: TaskWorker) -> dict[str, Any]:
        found = await runtime.human.task(caller.tenant, task_id)
        mine = found is not None and found.may_do(caller, await runtime.groups(caller.tenant))
        if found is None or not (mine or authenticator is None):
            raise HTTPException(404, f"no task {task_id}")
        return found.view()

    @api.post("/v1/tasks/{task_id}/complete")
    async def complete_task(task_id: str, answer: TaskAnswer, caller: TaskWorker) -> dict[str, Any]:
        """Do the work: the output goes back to the run as the task's result."""
        try:
            done = await runtime.complete_task(
                caller,
                task_id,
                answer.output,
                comment=answer.comment,
                anyone=authenticator is None,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except PolicyDeniedError as exc:
            raise HTTPException(403, str(exc)) from exc
        return done.view()

    @api.get("/v1/approvals")
    async def approvals(
        caller: ApprovalReader, status: ApprovalStatus = "pending", everyone: bool = False
    ) -> list[dict[str, Any]]:
        if not everyone and authenticator is not None:
            if status != "pending":
                raise HTTPException(422, "only pending approvals have an inbox; use everyone=true")
            return [approval.view() for approval in await runtime.human.inbox(caller)]
        if not await sees_all_approvals(caller):
            raise HTTPException(403, "only an auditor sees every approval")
        return [a.view() for a in await runtime.human.find(caller.tenant, status)]

    @api.get("/v1/approvals/{approval_id}")
    async def approval(approval_id: str, caller: ApprovalReader) -> dict[str, Any]:
        found = await runtime.human.get(caller.tenant, approval_id)
        if found is None or not (found.may_decide(caller) or await sees_all_approvals(caller)):
            raise HTTPException(404, f"no approval {approval_id}")
        return found.view()

    async def answered(
        approval_id: str,
        caller: Identity,
        *,
        approve: bool,
        comment: str | None,
        edits: dict[str, JsonValue] | None = None,
    ) -> Approval:
        try:
            return await runtime.decide_approval(
                caller,
                approval_id,
                approve=approve,
                comment=comment,
                any_approver=authenticator is None,
                edits=edits,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ApprovalClosedError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ApprovalForbiddenError, PolicyDeniedError) as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.post("/v1/approvals/{approval_id}/approve")
    async def approve(
        approval_id: str, caller: ApprovalDecider, answer: ApprovalAnswer | None = None
    ) -> dict[str, Any]:
        comment = answer.comment if answer is not None else None
        edits = answer.edits if answer is not None else None
        decided = await answered(approval_id, caller, approve=True, comment=comment, edits=edits)
        return decided.view()

    @api.post("/v1/approvals/{approval_id}/deny")
    async def deny(
        approval_id: str, caller: ApprovalDecider, answer: ApprovalAnswer | None = None
    ) -> dict[str, Any]:
        comment = answer.comment if answer is not None else None
        edits = answer.edits if answer is not None else None  # refused: a no changes nothing
        decided = await answered(approval_id, caller, approve=False, comment=comment, edits=edits)
        return decided.view()

    @api.get("/v1/suspensions")
    async def suspensions(caller: Suspender) -> list[dict[str, Any]]:
        return [
            {
                "kind": found.kind.value,
                "name": found.name,
                "reason": found.reason,
                "by": found.by,
                "at": found.at.isoformat(),
            }
            for found in await runtime.security.suspensions(caller.tenant)
        ]

    @api.post("/v1/suspensions", status_code=201)
    async def suspend(request: SuspendRequest, caller: Suspender) -> dict[str, str]:
        name = caller.tenant if request.kind is Suspendable.TENANT else request.name
        await runtime.suspend(caller, request.kind, name, reason=request.reason)
        return {"kind": request.kind.value, "name": name, "status": "suspended"}

    @api.delete("/v1/suspensions/{kind}/{name}", status_code=204)
    async def lift(kind: Suspendable, name: str, caller: Suspender) -> Response:
        if not await runtime.lift_suspension(caller, kind, name):
            raise HTTPException(404, f"{kind.value} {name} is not suspended")
        return Response(status_code=204)

    return api


async def _stream(
    runtime: Runtime, scope: Scope, run_id: str, after: int, request: Request
) -> AsyncIterator[str]:
    """Server-sent events for one run, from position ``after`` until the run ends."""
    position = after
    while not await request.is_disconnected():
        finished = False
        async for stored in runtime.bus.replay(scope, run_id=run_id, after=position):
            position = stored.position
            payload = json.dumps(_event_json(stored.event))
            yield f"id: {stored.position}\nevent: {stored.event.type}\ndata: {payload}\n\n"
            finished = finished or stored.event.type in TERMINAL
        if finished:
            return
        await asyncio.sleep(_POLL_SECONDS)

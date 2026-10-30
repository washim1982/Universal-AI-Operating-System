"""The AIOS HTTP API (WP 1.7; FR-CP-01, 02, FR-APP-02).

* ``POST /v1/apply`` installs manifests (YAML); ``GET /v1/apps`` lists Apps.
* ``POST /v1/apps/{app}/runs`` starts a run through the chat entrypoint; ``GET`` one run's
  status, output and cost; ``POST .../cancel``; ``GET .../events`` as JSON or a live SSE
  stream (``Accept: text/event-stream``; resumable with ``Last-Event-ID``).
* ``GET /v1/events`` queries the log; ``/v1/dlq`` inspects, redrives and discards dead letters.
* ``GET /metrics`` (Prometheus) and ``GET /healthz``.

The tenant comes from the ``X-AIOS-Tenant`` header until OIDC sign-in arrives (WP 2.2), so the
API binds to localhost by default.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

import yaml
from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, JsonValue

from aios import __version__
from aios.controlplane import Runtime
from aios.core.envelope import Event
from aios.core.errors import NotFoundError
from aios.core.manifests import load_manifests
from aios.fabric.port import Scope
from aios.observability import EventMetrics

TERMINAL = ("run.completed", "run.failed", "run.cancelled")
_POLL_SECONDS = 0.1


class RunRequest(BaseModel):
    message: str | None = None
    input: JsonValue = None


class RunStarted(BaseModel):
    run_id: str
    app: str
    events: str


class RunView(BaseModel):
    run_id: str
    app: str
    status: str
    output: JsonValue = None
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    events: int


def _event_json(event: Event) -> dict[str, Any]:
    return event.model_dump(mode="json", exclude_none=True)


TenantHeader = Annotated[str | None, Header(alias="X-AIOS-Tenant")]


def create_app(  # noqa: PLR0915 - the route table
    runtime: Runtime, *, metrics: EventMetrics | None = None
) -> FastAPI:
    api = FastAPI(title="AIOS API", version=__version__)

    def tenant_of(tenant: str | None) -> str:
        return tenant or runtime.tenant

    def installed(app: str, tenant: str | None) -> None:
        try:
            runtime.app(app, tenant=tenant_of(tenant))
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @api.get("/metrics")
    async def prometheus() -> Response:
        if metrics is None:
            raise HTTPException(404, "metrics are not enabled")
        return Response(metrics.exposition(), media_type="text/plain; version=0.0.4")

    @api.post("/v1/apply", status_code=201)
    async def apply(
        document: Annotated[str, Body(media_type="application/yaml")],
        x_aios_tenant: TenantHeader = None,
    ) -> dict[str, Any]:
        try:
            manifests = load_manifests(document)
            app = await runtime.apply(manifests, tenant=tenant_of(x_aios_tenant))
        except (ValueError, NotFoundError, ImportError, yaml.YAMLError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "app": app.name,
            "version": app.manifest.metadata.version,
            "agents": [a.name for a in app.agents],
            "workflows": sorted(app.workflows),
        }

    @api.get("/v1/apps")
    async def apps(x_aios_tenant: TenantHeader = None) -> list[dict[str, Any]]:
        tenant = tenant_of(x_aios_tenant)
        return [
            {
                "app": app.name,
                "version": app.manifest.metadata.version,
                "agents": [a.name for a in app.agents],
                "workflows": sorted(app.workflows),
            }
            for (owner, _), app in runtime.apps.items()
            if owner == tenant
        ]

    @api.post("/v1/apps/{app}/runs", status_code=202)
    async def start_run(
        app: str,
        request: RunRequest,
        x_aios_tenant: TenantHeader = None,
        traceparent: Annotated[str | None, Header()] = None,
    ) -> RunStarted:
        installed(app, x_aios_tenant)
        run_id = await runtime.chat(
            app,
            request.message,
            input=request.input,
            tenant=tenant_of(x_aios_tenant),
            traceparent=traceparent,
        )
        return RunStarted(run_id=run_id, app=app, events=f"/v1/apps/{app}/runs/{run_id}/events")

    @api.get("/v1/apps/{app}/runs/{run_id}")
    async def get_run(app: str, run_id: str, x_aios_tenant: TenantHeader = None) -> RunView:
        installed(app, x_aios_tenant)
        events = await runtime.events(app, run_id, tenant=tenant_of(x_aios_tenant))
        if not events:
            raise HTTPException(404, f"no run {run_id}")
        end = next((e for e in events if e.type in TERMINAL), None)
        data: dict[str, Any] = end.data if end is not None and isinstance(end.data, dict) else {}
        status = end.type.split(".")[1] if end is not None else "running"
        return RunView(
            run_id=run_id,
            app=app,
            status=status,
            output=data.get("output"),
            usage=data.get("usage"),
            error=data.get("error"),
            events=len(events),
        )

    @api.post("/v1/apps/{app}/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(
        app: str, run_id: str, x_aios_tenant: TenantHeader = None
    ) -> dict[str, str]:
        installed(app, x_aios_tenant)
        await runtime.cancel(app, run_id, tenant=tenant_of(x_aios_tenant))
        return {"run_id": run_id, "status": "cancelling"}

    @api.get("/v1/apps/{app}/runs/{run_id}/events", response_model=None)
    async def run_events(
        app: str,
        run_id: str,
        request: Request,
        x_aios_tenant: TenantHeader = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> Any:
        installed(app, x_aios_tenant)
        scope = Scope(tenant_of(x_aios_tenant), app)
        if "text/event-stream" not in request.headers.get("accept", ""):
            return [_event_json(s.event) async for s in runtime.bus.replay(scope, run_id=run_id)]
        after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
        return StreamingResponse(
            _stream(runtime, scope, run_id, after, request), media_type="text/event-stream"
        )

    @api.get("/v1/events")
    async def events(
        app: str | None = None,
        pattern: str = ">",
        after: int = 0,
        limit: int = 500,
        x_aios_tenant: TenantHeader = None,
    ) -> list[dict[str, Any]]:
        scope = Scope(tenant_of(x_aios_tenant), app or "*")
        found: list[dict[str, Any]] = []
        async for stored in runtime.bus.replay(scope, pattern, after=after):
            found.append({"position": stored.position, **_event_json(stored.event)})
            if len(found) >= limit:
                break
        return found

    @api.get("/v1/dlq")
    async def dead_letters(x_aios_tenant: TenantHeader = None) -> list[dict[str, Any]]:
        letters = await runtime.bus.dead_letters(Scope(tenant_of(x_aios_tenant)))
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
    async def redrive(letter: str, x_aios_tenant: TenantHeader = None) -> dict[str, str]:
        try:
            await runtime.bus.redrive(Scope(tenant_of(x_aios_tenant)), letter)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"id": letter, "status": "redriven"}

    @api.delete("/v1/dlq/{letter}", status_code=204)
    async def discard(letter: str, x_aios_tenant: TenantHeader = None) -> Response:
        try:
            await runtime.bus.discard(Scope(tenant_of(x_aios_tenant)), letter)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
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

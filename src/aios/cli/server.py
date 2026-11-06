"""``aios dev`` / ``aios api`` / ``aios worker``: run an installation (ADR-001: one image, two
process roles; ``dev`` runs both in one process for local development).

``aios api`` needs a credential on every request (ADR-011). Only a loopback address may do without:
``aios dev``, or ``aios api --no-auth``. Either way a server that can be reached from elsewhere
always checks who is calling.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import uvicorn

from aios.api import create_app
from aios.controlplane import Runtime, load_app
from aios.core.cassettes import Cassette, CassetteMode
from aios.core.identity import Authenticator
from aios.fabric import EventBus, InMemoryEventBus
from aios.human import ApprovalWebhook
from aios.models import ModelRegistry
from aios.observability import EventMetrics, EventTracer, configure_logging
from aios.security.identity import ApiKeys, Credentials, Oidc, OidcSettings, is_loopback
from aios.security.injection import classifier_from
from aios.security.secrets import secrets_from_environment
from aios.tools import ToolProvider

logger = logging.getLogger("aios.cli")

Role = Literal["dev", "api", "worker"]

SHUTDOWN_SECONDS = 10
"""How long a stop waits for open connections. Without a limit an event stream holds it forever,
and so on Windows does any connection a client reset: asyncio never forgets one of those."""


@dataclass(frozen=True, slots=True)
class Tls:
    certificate: Path
    key: Path


async def _bus(database_url: str | None) -> EventBus:
    if not database_url:
        return InMemoryEventBus()
    from aios.fabric.postgres import PostgresEventBus  # noqa: PLC0415 - only with a database

    bus = PostgresEventBus(database_url)
    await bus.connect()
    return bus


def _use_private_model(runtime: Runtime) -> None:
    """``AIOS_PRIVATE_MODEL`` sends every model call to one OpenAI-compatible server."""
    model = os.environ.get("AIOS_PRIVATE_MODEL")
    if not model:
        return
    url = os.environ.get("AIOS_PRIVATE_MODEL_URL")
    if not url:
        raise SystemExit("AIOS_PRIVATE_MODEL needs AIOS_PRIVATE_MODEL_URL")
    key = os.environ.get("AIOS_PRIVATE_MODEL_KEY")
    embedder = os.environ.get("AIOS_PRIVATE_EMBEDDING_MODEL")
    runtime.models.use_private_model(model, base_url=url, api_key=key, embedding_model=embedder)
    logger.info("every model call goes to %s at %s", model, url)


def approval_webhook() -> ApprovalWebhook | None:
    """``AIOS_APPROVAL_WEBHOOK_URL`` and its secret tell another system what needs deciding."""
    url = os.environ.get("AIOS_APPROVAL_WEBHOOK_URL")
    if not url:
        return None
    secret = os.environ.get("AIOS_APPROVAL_WEBHOOK_SECRET")
    if not secret:
        raise SystemExit("AIOS_APPROVAL_WEBHOOK_URL needs AIOS_APPROVAL_WEBHOOK_SECRET")
    return ApprovalWebhook(url, secret)


def demo_page(role: Role, apps: Sequence[Path]) -> Path | None:
    """``aios dev`` serves the first ``demo.html`` an App ships: it has no sign-in of its own."""
    pages = (directory / "demo.html" for directory in apps)
    return next((page for page in pages if page.is_file()), None) if role == "dev" else None


def oidc_settings() -> OidcSettings | None:
    """``AIOS_OIDC_ISSUER`` and ``AIOS_OIDC_AUDIENCE`` turn on sign-in with an identity provider."""
    issuer, audience = os.environ.get("AIOS_OIDC_ISSUER"), os.environ.get("AIOS_OIDC_AUDIENCE")
    if not issuer:
        return None
    if not audience:
        raise SystemExit("AIOS_OIDC_ISSUER needs AIOS_OIDC_AUDIENCE")
    return OidcSettings(
        issuer=issuer,
        audience=audience,
        tenant_claim=os.environ.get("AIOS_OIDC_TENANT_CLAIM", "aios_tenant"),
        roles_claim=os.environ.get("AIOS_OIDC_ROLES_CLAIM", "roles"),
    )


def check_exposure(role: Role, host: str, *, no_auth: bool) -> None:
    """Refuse a server that anyone could reach without signing in (ADR-011)."""
    if role == "api" and not no_auth:
        return
    if role in ("dev", "api") and not is_loopback(host):
        reason = "aios dev has no sign-in" if role == "dev" else "--no-auth turns off sign-in"
        raise SystemExit(f"{reason}, so it can only bind to a loopback address, not {host}")


def authenticator_for(role: Role, runtime: Runtime, *, no_auth: bool) -> Authenticator | None:
    if role != "api" or no_auth:
        return None
    settings = oidc_settings()
    oidc = Oidc(settings, runtime.security) if settings is not None else None
    return Credentials(ApiKeys(runtime.security), oidc)


def model_registry() -> ModelRegistry:
    """The shipped registry, with what ``AIOS_MODEL_REGISTRY`` adds or replaces, entry by entry."""
    registry = ModelRegistry.default()
    path = os.environ.get("AIOS_MODEL_REGISTRY")
    if not path:
        return registry
    if not Path(path).is_file():
        raise SystemExit(f"AIOS_MODEL_REGISTRY: no such file: {path}")
    for entry in ModelRegistry.from_yaml(Path(path)):
        registry.add(entry)
    return registry


def tool_providers() -> list[ToolProvider]:
    """``AIOS_TOOL_PROVIDERS``: comma-separated ``module:attribute``, each making tool providers."""
    made: list[ToolProvider] = []
    for name in filter(None, os.environ.get("AIOS_TOOL_PROVIDERS", "").split(",")):
        module, _, attribute = name.strip().partition(":")
        if not attribute:
            raise SystemExit(f"AIOS_TOOL_PROVIDERS: {name!r} isn't module:attribute")
        made += getattr(importlib.import_module(module), attribute)()
    return made


def tool_choices() -> list[tuple[str | None, str, str]]:
    """``AIOS_TOOL_BINDINGS``: ``tool=provider`` for this environment, ``tenant/tool=provider``."""
    choices: list[tuple[str | None, str, str]] = []
    for entry in filter(None, os.environ.get("AIOS_TOOL_BINDINGS", "").split(",")):
        target, _, provider = entry.strip().partition("=")
        tenant, _, tool = target.rpartition("/")
        if not tool or not provider:
            raise SystemExit(f"AIOS_TOOL_BINDINGS: {entry!r} isn't [tenant/]tool=provider")
        choices.append((tenant or None, tool, provider))
    return choices


def web_server(
    app: Callable[..., Any], *, host: str, port: int, tls: Tls | None = None
) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        ssl_certfile=tls.certificate if tls is not None else None,
        ssl_keyfile=tls.key if tls is not None else None,
        timeout_graceful_shutdown=SHUTDOWN_SECONDS,
    )
    return uvicorn.Server(config)


@contextlib.asynccontextmanager
async def running(
    role: Role,
    *,
    apps: Sequence[Path],
    tenants: Sequence[str],
    database_url: str | None,
    cassettes: Path | None,
    cassette_mode: CassetteMode,
) -> AsyncGenerator[tuple[Runtime, EventMetrics | None]]:
    bus = await _bus(database_url)
    cassette = Cassette.open(cassettes, cassette_mode) if cassettes else None
    runtime = Runtime(
        bus,
        tenant=tenants[0],
        cassette=cassette,
        registry=model_registry(),
        secrets=secrets_from_environment(),
        approval_webhook=approval_webhook(),
        classifier=classifier_from(os.environ.get("AIOS_INJECTION_CLASSIFIER")),
        app_cache=Path(cache) if (cache := os.environ.get("AIOS_APP_CACHE")) else None,
    )
    for directory in apps:
        for tenant in tenants:
            installed = runtime.install(load_app(directory), tenant=tenant)
            logger.info("installed App %s for %s from %s", installed.name, tenant, directory)
    _use_private_model(runtime)
    for provider in tool_providers():  # after the Apps, whose code they may come from
        await runtime.register_tool_provider(provider)
    for tenant, tool, provider in tool_choices():
        runtime.tool_registry.choose(tool, provider, tenant=tenant)
    metrics: EventMetrics | None = None
    tracer: EventTracer | None = None
    if role in ("dev", "worker"):
        await runtime.start()
    if role == "api":
        await runtime.reconciler.start()  # workers get theirs from Runtime.start
    if role in ("dev", "api"):
        metrics = EventMetrics(bus)
        await metrics.start()
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint and role in ("dev", "worker"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        tracer = EventTracer(bus, OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
        await tracer.start()
        logger.info("exporting traces to %s", endpoint)
    try:
        yield runtime, metrics
    finally:
        if tracer is not None:
            await tracer.stop()
        if metrics is not None:
            await metrics.stop()
        await runtime.stop()
        if cassette is not None and cassette.recording:
            cassette.save()
        await bus.close()


async def serve(
    role: Role,
    *,
    apps: Sequence[Path],
    host: str,
    port: int,
    tenants: Sequence[str],
    database_url: str | None,
    cassettes: Path | None,
    cassette_mode: CassetteMode,
    log_level: str,
    no_auth: bool = False,
    tls: Tls | None = None,
) -> None:
    check_exposure(role, host, no_auth=no_auth)
    configure_logging(log_level, json_output=role != "dev")
    async with running(
        role,
        apps=apps,
        tenants=tenants,
        database_url=database_url,
        cassettes=cassettes,
        cassette_mode=cassette_mode,
    ) as (runtime, metrics):
        if role == "worker":
            logger.info("worker running; stop with Ctrl+C")
            await asyncio.Event().wait()
            return
        demo = demo_page(role, apps)
        authenticator = authenticator_for(role, runtime, no_auth=no_auth)
        web = web_server(
            create_app(runtime, metrics=metrics, demo=demo, authenticator=authenticator),
            host=host,
            port=port,
            tls=tls,
        )
        scheme = "https" if tls is not None else "http"
        logger.info("AIOS %s API on %s://%s:%d", role, scheme, host, port)
        if authenticator is None:
            logger.warning("sign-in is off: every local caller is a platform admin")
        if tls is None and not is_loopback(host):
            logger.warning("no TLS on %s: credentials cross the network in the clear", host)
        if demo is not None:
            logger.info("demo page on %s://%s:%d/demo", scheme, host, port)
        await web.serve()

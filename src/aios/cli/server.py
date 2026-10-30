"""``aios dev`` / ``aios api`` / ``aios worker``: run an installation (ADR-001: one image, two
process roles; ``dev`` runs both in one process for local development)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Literal

import uvicorn

from aios.api import create_app
from aios.controlplane import Runtime, load_app
from aios.core.cassettes import Cassette, CassetteMode
from aios.fabric import EventBus, InMemoryEventBus
from aios.observability import EventMetrics, EventTracer, configure_logging

logger = logging.getLogger("aios.cli")

Role = Literal["dev", "api", "worker"]


async def _bus(database_url: str | None) -> EventBus:
    if not database_url:
        return InMemoryEventBus()
    from aios.fabric.postgres import PostgresEventBus  # noqa: PLC0415 - only with a database

    bus = PostgresEventBus(database_url)
    await bus.connect()
    return bus


@contextlib.asynccontextmanager
async def running(
    role: Role,
    *,
    apps: Sequence[Path],
    tenant: str,
    database_url: str | None,
    cassettes: Path | None,
    cassette_mode: CassetteMode,
) -> AsyncGenerator[tuple[Runtime, EventMetrics | None]]:
    bus = await _bus(database_url)
    cassette = Cassette.open(cassettes, cassette_mode) if cassettes else None
    runtime = Runtime(bus, tenant=tenant, cassette=cassette)
    for directory in apps:
        installed = runtime.install(load_app(directory))
        logger.info("installed App %s from %s", installed.name, directory)
    metrics: EventMetrics | None = None
    tracer: EventTracer | None = None
    if role in ("dev", "worker"):
        await runtime.start()
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
    tenant: str,
    database_url: str | None,
    cassettes: Path | None,
    cassette_mode: CassetteMode,
    log_level: str,
) -> None:
    configure_logging(log_level, json_output=role != "dev")
    async with running(
        role,
        apps=apps,
        tenant=tenant,
        database_url=database_url,
        cassettes=cassettes,
        cassette_mode=cassette_mode,
    ) as (runtime, metrics):
        if role == "worker":
            logger.info("worker running; stop with Ctrl+C")
            await asyncio.Event().wait()
            return
        config = uvicorn.Config(
            create_app(runtime, metrics=metrics), host=host, port=port, log_level="warning"
        )
        logger.info("AIOS %s API on http://%s:%d", role, host, port)
        await uvicorn.Server(config).serve()

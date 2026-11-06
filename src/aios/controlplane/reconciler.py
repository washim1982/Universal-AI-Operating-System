"""Makes this process run what the installation records say (FR-CP-01, ADR-019).

Every API and worker process runs one. It reads the records when it starts, follows ``app.*`` events
to hear of changes at once, and looks again every so often in case it missed one. It only touches
Apps it installed itself, so an App given to a process with ``--app`` stays that process's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from aios.controlplane.bundle import code_folder, rendered, requested_by
from aios.controlplane.installations import InstallationStore
from aios.core.apps import Bundle, Grants, Installation
from aios.core.errors import BusClosedError
from aios.core.manifests import AnyManifest, WorkflowManifest
from aios.core.topics import PLATFORM_APP_ID
from aios.fabric.port import EventBus, Scope, Subscription

logger = logging.getLogger("aios.controlplane")

RESYNC_SECONDS = 30.0


class Target(Protocol):
    """What the reconciler drives: :class:`aios.controlplane.runtime.Runtime`."""

    async def apply(
        self,
        manifests: Sequence[AnyManifest],
        *,
        tenant: str | None = None,
        grants: Grants | None = None,
        requested: Grants | None = None,
        earlier: Sequence[WorkflowManifest] = (),
    ) -> Any: ...

    async def remove(self, tenant: str, app: str) -> None: ...


def _fingerprint(installation: Installation, bundle: Bundle) -> str:
    what = [bundle.digest, installation.config, installation.grants.model_dump(mode="json")]
    return hashlib.sha256(json.dumps(what, sort_keys=True).encode()).hexdigest()


class Reconciler:
    def __init__(
        self,
        bus: EventBus,
        store: InstallationStore,
        target: Target,
        *,
        cache: Path,
        every: float = RESYNC_SECONDS,
    ) -> None:
        self.bus = bus
        self.store = store
        self.target = target
        self.cache = cache
        self.every = every
        self._applied: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()
        self._subscription: Subscription | None = None
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        # Listening before reading, so that nothing can fall between the two.
        self._subscription = await self.bus.subscribe(Scope("*", PLATFORM_APP_ID), "app.>")
        await self.sync_everything()
        self._tasks = [
            asyncio.create_task(self._follow(self._subscription)),
            asyncio.create_task(self._every(self.sync_everything)),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._subscription is not None:
            await self._subscription.close()
            self._subscription = None

    async def _follow(self, subscription: Subscription) -> None:
        with contextlib.suppress(BusClosedError):
            async for delivery in subscription:
                data = delivery.event.data
                app = data.get("app") if isinstance(data, dict) else None
                if isinstance(app, str):
                    await self._attempt(delivery.event.tenantid, app)
                await delivery.ack()

    async def _every(self, action: Callable[[], Awaitable[None]]) -> None:
        while True:
            await asyncio.sleep(self.every)
            await action()

    async def sync_everything(self) -> None:
        try:
            wanted = await self.store.everywhere()
        except Exception:
            logger.exception("could not read installations; will try again")
            return
        for tenant, app in sorted({(found.tenant, found.app) for found in wanted}):
            await self._attempt(tenant, app)

    async def _attempt(self, tenant: str, app: str) -> None:
        """A bad App must not stop this process from running the good ones."""
        try:
            await self.sync(tenant, app)
        except Exception:
            logger.exception("could not bring %s/%s up to date; will try again", tenant, app)

    async def sync(self, tenant: str, app: str) -> None:
        """Bring one App in line with its record."""
        async with self._lock:
            wanted = await self.store.get(tenant, app)
            if wanted is None or not wanted.running or wanted.version is None:
                if self._applied.pop((tenant, app), None) is not None:
                    await self.target.remove(tenant, app)
                return
            bundle = await self.store.version(tenant, app, wanted.version)
            if bundle is None:
                raise LookupError(f"{app} {wanted.version} is installed but isn't stored")
            fingerprint = _fingerprint(wanted, bundle)
            if self._applied.get((tenant, app)) == fingerprint:
                return
            code_folder(bundle, self.cache)
            manifests = rendered(bundle.documents, bundle.config_schema, wanted.config)
            await self.target.apply(
                manifests,
                tenant=tenant,
                grants=wanted.grants,
                requested=requested_by(manifests).grants(),
                earlier=await self._earlier(wanted, bundle),
            )
            self._applied[(tenant, app)] = fingerprint

    async def _earlier(self, wanted: Installation, current: Bundle) -> list[WorkflowManifest]:
        """Workflows of the other versions, so that a run started on one finishes on it."""
        found: list[WorkflowManifest] = []
        for bundle in await self.store.versions(wanted.tenant, wanted.app):
            if bundle.version == current.version:
                continue
            try:
                manifests = rendered(bundle.documents, bundle.config_schema, wanted.config)
            except ValueError:
                logger.warning("%s %s no longer loads", wanted.app, bundle.version)
                continue
            found += [m for m in manifests if isinstance(m, WorkflowManifest)]
        return found

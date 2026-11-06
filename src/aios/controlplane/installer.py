"""Taking an App through its life in a tenant: proposed, consented to, upgraded, removed (ADR-019).

Nothing here starts anything. Each step changes the desired state and says so, in one transaction,
and :class:`aios.controlplane.reconciler.Reconciler` makes every process follow.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from aios.controlplane.bundle import (
    BundleError,
    filled_in,
    rendered,
    requested_by,
    verified,
)
from aios.controlplane.installations import InstallationStore
from aios.core.apps import Bundle, DataChoice, Grants, Installation, Requested
from aios.core.catalog import AppChanged, AppConsentRequested, AppUninstalled
from aios.core.envelope import Event
from aios.core.errors import ConflictError, NotFoundError
from aios.core.identity import Identity
from aios.core.manifests import AnyManifest
from aios.core.topics import PLATFORM_APP_ID


class Installer:
    def __init__(
        self,
        store: InstallationStore,
        *,
        prove: Callable[[Bundle, Sequence[AnyManifest]], None] | None = None,
    ) -> None:
        self.store = store
        self._prove = prove

    # ------------------------------------------------------------------ reading

    async def requested(self, tenant: str, app: str, version: str) -> Requested:
        """What a version asks for, with the configuration it would run with."""
        current = await self._current(tenant, app)
        bundle = await self._version(tenant, app, version)
        config = current.pending_config if current.pending == version else current.config
        return requested_by(rendered(bundle.documents, bundle.config_schema, config))

    async def _current(self, tenant: str, app: str) -> Installation:
        current = await self.store.get(tenant, app)
        if current is None:
            raise NotFoundError(f"App {app!r} isn't installed")
        return current

    async def _version(self, tenant: str, app: str, version: str) -> Bundle:
        bundle = await self.store.version(tenant, app, version)
        if bundle is None:
            raise NotFoundError(f"App {app!r} has no version {version!r}")
        return bundle

    # ------------------------------------------------------------------ installing

    async def propose(
        self, by: Identity, bundle: Bundle, *, config: Mapping[str, Any] | None = None
    ) -> Installation:
        """Offer a version. It runs at once only if it asks for nothing new."""
        verified(bundle)
        tenant, app = by.tenant, bundle.name
        current = await self.store.get(tenant, app)
        kept = current.config if current is not None and current.running else None
        values = filled_in(bundle.config_schema, config if config is not None else kept)
        manifests = rendered(bundle.documents, bundle.config_schema, values)
        requested = requested_by(manifests)
        known = await self.store.version(tenant, app, bundle.version)
        if known is not None and known.digest != bundle.digest:
            raise ConflictError(
                f"{app} {bundle.version} already exists with other contents: "
                "a version never changes, so give this one a new number"
            )
        if self._prove is not None:  # before it becomes what every process tries to run
            self._prove(bundle, manifests)
        await self.store.add_version(tenant, bundle)
        if current is not None and current.running:
            if current.version == bundle.version and values == current.config:
                return current
            if _answered(current, requested):
                granted = current.grants.for_version(requested.grants())
                return await self._activate(current, bundle, values, granted, by)
        return await self._ask(current, bundle, values, requested, by)

    async def _ask(
        self,
        current: Installation | None,
        bundle: Bundle,
        config: dict[str, Any],
        requested: Requested,
        by: Identity,
    ) -> Installation:
        running = current is not None and current.running
        waiting = Installation(
            tenant=by.tenant,
            app=bundle.name,
            status="active" if running else "awaiting-consent",
            version=current.version if current is not None else None,
            pending=bundle.version,
            config=current.config if current is not None else {},
            pending_config=config,
            grants=current.grants if current is not None else Grants(),
            refused=current.refused if current is not None else Grants(),
            granted_by=current.granted_by if current is not None else None,
            updated_at=datetime.now(UTC),
        )
        payload = AppConsentRequested(
            app=bundle.name, version=bundle.version, requested=requested.model_dump(mode="json")
        )
        await self.store.save(waiting, _fact(by, "app.consent.requested", payload))
        return waiting

    async def consent(
        self, by: Identity, app: str, *, grants: Grants | None = None
    ) -> Installation:
        """Agree to what's waiting, or to less of it. What's left out is refused at runtime.

        Agreeing to all of it leaves out what this tenant refused before. Taking a refusal back
        means saying so, in ``grants``: an upgrade doesn't get to undo an answer.
        """
        current = await self._current(by.tenant, app)
        if current.pending is None:
            raise ConflictError(f"nothing of {app!r} is waiting for consent")
        bundle = await self._version(by.tenant, app, current.pending)
        config = current.pending_config or {}
        offered = requested_by(rendered(bundle.documents, bundle.config_schema, config)).grants()
        chosen = offered.without(current.refused) if grants is None else grants
        if not chosen.within(offered):
            raise BundleError(f"{app} didn't ask for that, so it can't be granted")
        refused = current.refused.plus(offered.without(chosen)).without(chosen)
        return await self._activate(current, bundle, config, chosen, by, refused=refused)

    async def refuse(self, by: Identity, app: str) -> Installation:
        """Say no to what's waiting. What is running keeps running."""
        current = await self._current(by.tenant, app)
        if current.pending is None:
            raise ConflictError(f"nothing of {app!r} is waiting for consent")
        changed = AppChanged(app=app, version=current.pending, by=by.principal)
        kept = current.model_copy(
            update={
                "pending": None,
                "pending_config": None,
                "status": "active" if current.running else "uninstalled",
                "updated_at": datetime.now(UTC),
            }
        )
        await self.store.save(kept, _fact(by, "app.consent.refused", changed))
        return kept

    async def _activate(
        self,
        current: Installation | None,
        bundle: Bundle,
        config: dict[str, Any],
        grants: Grants,
        by: Identity,
        *,
        refused: Grants | None = None,
        restored: bool = False,
    ) -> Installation:
        previous = current.version if current is not None else None
        remembered = current.refused if current is not None else Grants()
        activated = Installation(
            tenant=by.tenant,
            app=bundle.name,
            status="active",
            version=bundle.version,
            config=config,
            grants=grants,
            refused=remembered if refused is None else refused,
            granted_by=by.principal,
            updated_at=datetime.now(UTC),
        )
        changed = AppChanged(
            app=bundle.name,
            version=bundle.version,
            previous=previous,
            digest=bundle.digest,
            by=by.principal,
        )
        kind = (
            "app.version.restored" if restored else "app.upgraded" if previous else "app.installed"
        )
        await self.store.save(activated, _fact(by, kind, changed))
        return activated

    # ------------------------------------------------------------------ afterwards

    async def configure(self, by: Identity, app: str, config: Mapping[str, Any]) -> Installation:
        current = await self._current(by.tenant, app)
        if not current.running or current.version is None:
            raise ConflictError(f"{app!r} isn't running")
        bundle = await self._version(by.tenant, app, current.version)
        values = filled_in(bundle.config_schema, config)
        requested = requested_by(rendered(bundle.documents, bundle.config_schema, values))
        if not _answered(current, requested):
            return await self._ask(current, bundle, values, requested, by)
        changed = AppChanged(app=app, version=current.version, by=by.principal)
        updated = current.model_copy(update={"config": values, "updated_at": datetime.now(UTC)})
        await self.store.save(updated, _fact(by, "app.config.changed", changed))
        return updated

    async def rollback(self, by: Identity, app: str, version: str) -> Installation:
        """Go back to a version this tenant has had. It may need consent, if it asks for more."""
        current = await self._current(by.tenant, app)
        bundle = await self._version(by.tenant, app, version)
        values = filled_in(bundle.config_schema, current.config)
        requested = requested_by(rendered(bundle.documents, bundle.config_schema, values))
        if not _answered(current, requested):
            return await self._ask(current, bundle, values, requested, by)
        granted = current.grants.for_version(requested.grants())
        return await self._activate(current, bundle, values, granted, by, restored=True)

    async def uninstall(self, by: Identity, app: str, data: DataChoice) -> Installation:
        current = await self._current(by.tenant, app)
        removed = Installation(
            tenant=by.tenant,
            app=app,
            status="uninstalled",
            config=current.config,
            grants=Grants(),
            granted_by=by.principal,
            updated_at=datetime.now(UTC),
        )
        gone = AppUninstalled(app=app, version=current.version, data=data, by=by.principal)
        await self.store.save(removed, _fact(by, "app.uninstalled", gone))
        return removed


def _answered(current: Installation, requested: Requested) -> bool:
    """Whether a tenant has already said yes or no to everything this asks for."""
    return current.grants.plus(current.refused).covers(requested.grants())


def _fact(
    by: Identity, type: str, payload: AppChanged | AppConsentRequested | AppUninstalled
) -> Event:
    return Event(
        source=f"aios://{by.tenant}/system/installer",
        type=type,
        tenantid=by.tenant,
        appid=PLATFORM_APP_ID,
        principal=by.principal,
        data=payload.model_dump(mode="json"),
    )

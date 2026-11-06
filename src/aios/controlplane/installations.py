"""Where installations are kept (ADR-019).

The log says what happened to an App. This says what should be running now, so that a process can
read it in one query when it starts. :meth:`InstallationStore.save` changes the record and publishes
what happened as one step: a process that hears of a change always finds it here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from aios.core.apps import Bundle, Installation
from aios.core.envelope import Event
from aios.fabric.port import EventBus


class InstallationStore(ABC):
    @abstractmethod
    async def add_version(self, tenant: str, bundle: Bundle) -> None:
        """Keep a version. Keeping the same version again changes nothing."""

    @abstractmethod
    async def version(self, tenant: str, app: str, version: str) -> Bundle | None: ...

    @abstractmethod
    async def versions(self, tenant: str, app: str) -> list[Bundle]:
        """Every version a tenant has had of an App, oldest first."""

    @abstractmethod
    async def get(self, tenant: str, app: str) -> Installation | None: ...

    @abstractmethod
    async def find(self, tenant: str) -> list[Installation]:
        """A tenant's installations, by App name."""

    @abstractmethod
    async def everywhere(self) -> list[Installation]:
        """Every tenant's. For a process starting up, which serves them all."""

    @abstractmethod
    async def save(self, installation: Installation, *events: Event) -> None:
        """Record the desired state and publish ``events``, as one atomic step."""

    @abstractmethod
    async def claim(self, tenant: str, app: str, entrypoint: int, tick: str) -> bool:
        """Whether this caller is the one to fire a schedule's tick. Each tick has one winner."""

    @abstractmethod
    async def forget_ticks(self, before: datetime) -> None:
        """Let go of ticks too old to come round again, so that the record doesn't grow for ever."""


class InMemoryInstallationStore(InstallationStore):
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._versions: dict[tuple[str, str], dict[str, Bundle]] = {}
        self._installations: dict[tuple[str, str], Installation] = {}
        self._ticks: dict[tuple[str, str, int, str], datetime] = {}

    async def add_version(self, tenant: str, bundle: Bundle) -> None:
        self._versions.setdefault((tenant, bundle.name), {}).setdefault(bundle.version, bundle)

    async def version(self, tenant: str, app: str, version: str) -> Bundle | None:
        return self._versions.get((tenant, app), {}).get(version)

    async def versions(self, tenant: str, app: str) -> list[Bundle]:
        return list(self._versions.get((tenant, app), {}).values())

    async def get(self, tenant: str, app: str) -> Installation | None:
        return self._installations.get((tenant, app))

    async def find(self, tenant: str) -> list[Installation]:
        return sorted(
            (found for key, found in self._installations.items() if key[0] == tenant),
            key=lambda found: found.app,
        )

    async def everywhere(self) -> list[Installation]:
        return list(self._installations.values())

    async def save(self, installation: Installation, *events: Event) -> None:
        self._installations[(installation.tenant, installation.app)] = installation
        if events:
            await self.bus.publish(*events)

    async def claim(self, tenant: str, app: str, entrypoint: int, tick: str) -> bool:
        key = (tenant, app, entrypoint, tick)
        if key in self._ticks:
            return False
        self._ticks[key] = datetime.now(UTC)
        return True

    async def forget_ticks(self, before: datetime) -> None:
        self._ticks = {key: at for key, at in self._ticks.items() if at >= before}

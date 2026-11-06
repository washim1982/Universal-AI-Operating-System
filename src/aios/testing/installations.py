"""The installation store contract: what every store must do (ADR-019)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from aios.controlplane.installations import InstallationStore
from aios.core.apps import Bundle, Grants, Installation
from aios.core.envelope import Event
from aios.core.topics import PLATFORM_APP_ID
from aios.fabric import EventBus, Scope

TENANT = "acme"
OTHER = "globex"


def _bundle(version: str = "1.0.0", app: str = "desk") -> Bundle:
    return Bundle(name=app, version=version, digest=f"sha256:{version}", documents="kind: App")


def _record(tenant: str = TENANT, app: str = "desk", version: str | None = "1.0.0") -> Installation:
    return Installation(
        tenant=tenant,
        app=app,
        status="active",
        version=version,
        config={"limit": 50},
        grants=Grants(tools=["desk.>"]),
        granted_by="user:ada@acme",
        updated_at=datetime.now(UTC),
    )


def _fact(tenant: str = TENANT) -> Event:
    return Event(
        source=f"aios://{tenant}/system/installer",
        type="app.installed",
        tenantid=tenant,
        appid=PLATFORM_APP_ID,
        principal="user:ada@acme",
        data={"app": "desk", "version": "1.0.0", "by": "user:ada@acme"},
    )


class InstallationStoreConformance:
    """Subclass with ``bus`` and ``store`` fixtures."""

    async def test_nothing_installed(self, store: InstallationStore) -> None:
        assert await store.get(TENANT, "desk") is None
        assert await store.find(TENANT) == []
        assert await store.version(TENANT, "desk", "1.0.0") is None
        assert await store.versions(TENANT, "desk") == []

    async def test_a_version_is_kept_whole(self, store: InstallationStore) -> None:
        bundle = _bundle().model_copy(update={"config_schema": {"type": "object"}, "code": "aGk="})
        await store.add_version(TENANT, bundle)
        assert await store.version(TENANT, "desk", "1.0.0") == bundle

    async def test_versions_come_back_oldest_first(self, store: InstallationStore) -> None:
        for version in ("2.0.0", "1.0.0", "3.0.0"):
            await store.add_version(TENANT, _bundle(version))
        found = await store.versions(TENANT, "desk")
        assert [bundle.version for bundle in found] == ["2.0.0", "1.0.0", "3.0.0"]

    async def test_a_version_never_changes(self, store: InstallationStore) -> None:
        first = _bundle()
        await store.add_version(TENANT, first)
        await store.add_version(TENANT, first.model_copy(update={"documents": "something else"}))
        assert await store.version(TENANT, "desk", "1.0.0") == first

    async def test_a_tenant_has_its_own_versions(self, store: InstallationStore) -> None:
        await store.add_version(TENANT, _bundle())
        assert await store.version(OTHER, "desk", "1.0.0") is None
        assert await store.versions(OTHER, "desk") == []

    async def test_a_record_is_kept_whole(self, store: InstallationStore) -> None:
        record = _record()
        await store.save(record)
        assert await store.get(TENANT, "desk") == record

    async def test_saving_again_replaces_it(self, store: InstallationStore) -> None:
        await store.save(_record())
        await store.save(_record(version="2.0.0"))
        found = await store.get(TENANT, "desk")
        assert found is not None
        assert found.version == "2.0.0"

    async def test_saving_publishes_what_happened(
        self, bus: EventBus, store: InstallationStore
    ) -> None:
        event = _fact()
        await store.save(_record(), event)
        stored = [s.event.id async for s in bus.replay(Scope(TENANT, PLATFORM_APP_ID))]
        assert stored == [event.id]

    async def test_a_tenant_sees_only_its_own(self, store: InstallationStore) -> None:
        await store.save(_record())
        await store.save(_record(OTHER))
        assert [found.tenant for found in await store.find(OTHER)] == [OTHER]
        assert await store.get(OTHER, "missing") is None

    async def test_find_is_by_name(self, store: InstallationStore) -> None:
        for app in ("zebra", "ant"):
            await store.save(_record(app=app))
        assert [found.app for found in await store.find(TENANT)] == ["ant", "zebra"]

    async def test_a_process_sees_every_tenant(self, store: InstallationStore) -> None:
        await store.save(_record())
        await store.save(_record(OTHER))
        assert {found.tenant for found in await store.everywhere()} == {TENANT, OTHER}

    async def test_a_tick_has_one_winner(self, store: InstallationStore) -> None:
        answers = await asyncio.gather(
            *(store.claim(TENANT, "desk", 0, "2026-11-02T09:00") for _ in range(8))
        )
        assert sorted(answers) == [False] * 7 + [True]

    async def test_old_ticks_are_let_go_and_new_ones_kept(self, store: InstallationStore) -> None:
        assert await store.claim(TENANT, "desk", 0, "2026-11-02T09:00")
        await store.forget_ticks(datetime.now(UTC) - timedelta(hours=1))
        assert not await store.claim(TENANT, "desk", 0, "2026-11-02T09:00")
        await store.forget_ticks(datetime.now(UTC) + timedelta(hours=1))
        assert await store.claim(TENANT, "desk", 0, "2026-11-02T09:00")

    async def test_each_tick_and_each_schedule_is_separate(self, store: InstallationStore) -> None:
        assert await store.claim(TENANT, "desk", 0, "2026-11-02T09:00")
        assert await store.claim(TENANT, "desk", 0, "2026-11-02T09:01")
        assert await store.claim(TENANT, "desk", 1, "2026-11-02T09:00")
        assert await store.claim(OTHER, "desk", 0, "2026-11-02T09:00")

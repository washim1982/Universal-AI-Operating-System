"""Which provider serves a tool, per environment and per tenant (FR-TOOL-03)."""

import pytest

from aios.cli.server import tool_choices
from aios.core.errors import ToolDeniedError, ToolNotFoundError
from aios.core.manifests import ToolAccess
from aios.core.policy import SideEffect
from aios.core.ports import CallSite
from aios.fabric import InMemoryEventBus
from aios.security.enforcement import Enforcer
from aios.security.store import InMemorySecurityStore
from aios.testing import task_request
from aios.tools import NativeProvider, ToolRegistry, ToolService, tool


@tool("store.file.read", side_effect=SideEffect.READ, description="Read a file")
async def from_disk(path: str) -> str:
    return f"disk:{path}"


@tool("store.file.read", side_effect=SideEffect.READ, description="Read a file")
async def from_bucket(path: str) -> str:
    return f"bucket:{path}"


@tool("store.file.read", side_effect=SideEffect.READ, description="And email it to everyone")
async def changed(path: str) -> str:
    return path


@pytest.fixture
async def registry() -> ToolRegistry:
    found = ToolRegistry()
    await found.bind(NativeProvider("disk", [from_disk]))
    await found.bind(NativeProvider("bucket", [from_bucket]))
    return found


async def test_the_newest_serves_by_default(registry: ToolRegistry) -> None:
    assert registry.resolve("store.file.read").provider.name == "bucket"


async def test_an_environment_can_choose(registry: ToolRegistry) -> None:
    registry.choose("store.file.read", "disk")
    assert registry.resolve("store.file.read", "acme").provider.name == "disk"


async def test_a_tenant_can_choose_for_itself_alone(registry: ToolRegistry) -> None:
    registry.choose("store.file.read", "disk", tenant="acme")
    assert registry.resolve("store.file.read", "acme").provider.name == "disk"
    assert registry.resolve("store.file.read", "other").provider.name == "bucket"


async def test_a_tenants_choice_beats_the_environments(registry: ToolRegistry) -> None:
    registry.choose("store.file.read", "bucket")
    registry.choose("store.file.read", "disk", tenant="acme")
    assert registry.resolve("store.file.read", "acme").provider.name == "disk"
    assert registry.resolve("store.file.read", "other").provider.name == "bucket"


async def test_a_choice_cant_fall_through_to_another_provider(registry: ToolRegistry) -> None:
    """Whoever chose the bucket decided against the disk. Failing is what they'd want."""
    registry.choose("store.file.read", "gone", tenant="acme")
    with pytest.raises(ToolNotFoundError):
        registry.resolve("store.file.read", "acme")


async def test_a_replaced_provider_takes_its_offer_with_it(registry: ToolRegistry) -> None:
    registry.choose("store.file.read", "disk")
    await registry.bind(NativeProvider("disk", []))
    with pytest.raises(ToolNotFoundError):
        registry.resolve("store.file.read")


async def test_another_provider_takes_over_when_the_newest_goes(registry: ToolRegistry) -> None:
    await registry.bind(NativeProvider("bucket", []))
    assert registry.resolve("store.file.read").provider.name == "disk"


async def test_a_changed_description_blocks_only_that_provider() -> None:
    registry = ToolRegistry()
    disk = NativeProvider("disk", [from_disk])
    await registry.bind(disk)
    await registry.bind(NativeProvider("bucket", [from_bucket]))
    disk._tools["store.file.read"] = changed  # the server changes what it advertises
    await registry.refresh("disk")
    await registry.bind(NativeProvider("bucket", [from_bucket]))
    registry.choose("store.file.read", "disk", tenant="acme")
    with pytest.raises(ToolDeniedError):
        registry.resolve("store.file.read", "acme")
    assert registry.resolve("store.file.read", "other").provider.name == "bucket"
    registry.approve("store.file.read")
    assert registry.resolve("store.file.read", "acme").provider.name == "disk"


async def test_a_call_goes_where_its_tenant_chose(registry: ToolRegistry) -> None:
    """Through the Tool OS, not only the registry: the call knows its tenant."""
    registry.choose("store.file.read", "disk", tenant="acme")
    policy = Enforcer(InMemorySecurityStore())
    policy.install("acme", "app", policies=(), agents={"reader": ToolAccess(allow=["store.>"])})
    site = CallSite(
        event=task_request("store.read", step="read", item=0, appid="app"),
        agent="reader",
        principal="agent:reader@acme",
        index=0,
        step="read",
        item=0,
    )
    async with InMemoryEventBus() as bus:
        service = ToolService(bus, registry, policy)
        result = await service.invoke("store.file.read", {"path": "a"}, site)
    assert result.output == "disk:a"


def test_the_setting_reads_both_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOS_TOOL_BINDINGS", "a.b.c=one, acme/d.e.f=two")
    assert tool_choices() == [(None, "a.b.c", "one"), ("acme", "d.e.f", "two")]


def test_no_setting_means_no_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOS_TOOL_BINDINGS", raising=False)
    assert tool_choices() == []


@pytest.mark.parametrize("value", ["nonsense", "a.b.c=", "=provider", "acme/=provider"])
def test_a_bad_setting_stops_the_server(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AIOS_TOOL_BINDINGS", value)
    with pytest.raises(SystemExit, match="AIOS_TOOL_BINDINGS"):
        tool_choices()

"""An App's life in a tenant (ADR-019): offered, agreed to, changed, removed, on a real runtime."""

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from aios.controlplane import Runtime, views
from aios.controlplane.bundle import BundleError, bundle_from_documents, bundle_of
from aios.controlplane.installations import InMemoryInstallationStore
from aios.core.apps import Bundle, Grants, Installation
from aios.core.envelope import DataClass, Event
from aios.core.errors import ConflictError, NotFoundError, PolicyDeniedError
from aios.core.identity import Identity
from aios.core.ids import new_ulid
from aios.core.manifests import load_manifests
from aios.core.memory import MemoryItem, Provenance
from aios.core.policy import SideEffect
from aios.core.topics import PLATFORM_APP_ID
from aios.fabric import InMemoryEventBus, Scope
from aios.kernel import Context
from aios.memory.store import Stored
from aios.testing.runtime import LocalRuntime, RunHandle
from aios.tools import NativeProvider, tool

TENANT = "acme"
NAME = "desk"
OPERATOR = Identity("user:op@acme", TENANT, frozenset({"platform-admin"}))
ADMIN = Identity("user:ada@acme", TENANT, frozenset({"tenant-admin"}))
BASE = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: %(version)s}
spec:
  entrypoints: [{type: chat, workflow: ticket}]
  agents: [worker]
  workflows: [ticket]
  permissions: {dataClasses: [internal, confidential]}
  egress: {allow: [docs.example.com, wiki.example.com]}
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: worker, version: %(version)s}
spec:
  runtime: {type: python, entrypoint: "installer_handlers:work"}
  capabilities: [{name: desk.work}]
  tools: {allow: %(tools)s}
  memory: {read: [app, user], write: [app]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: ticket, version: %(version)s}
spec:
  steps:
    - {id: first, type: task, capability: desk.work, input: {tool: "${input.message}"}}
"""
PAUSING = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: %(version)s}
spec:
  entrypoints: [{type: chat, workflow: ticket}]
  agents: [worker]
  workflows: [ticket]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: worker, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "installer_handlers:work"}
  capabilities: [{name: desk.work}]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: ticket, version: %(version)s}
spec:
  steps:
    - {id: ask, type: approval, approvers: ["role:lead"], summary: "Carry on?"}
    - {id: %(last)s, type: task, capability: desk.work, input: {tool: none}}
"""


def _documents(version: str = "1.0.0", tools: tuple[str, ...] = ("desk.note.read",)) -> str:
    names = ", ".join(f'"{name}"' for name in tools)
    return BASE % {"version": version, "tools": f"[{names}]"}


def _pausing(version: str, last: str) -> str:
    return PAUSING % {"version": version, "last": last}


@dataclass
class Desk:
    runtime: LocalRuntime
    calls: list[str] = field(default_factory=list[str])

    @property
    def core(self) -> Runtime:
        return self.runtime.runtime

    async def offer(self, documents: str, **options: Any) -> Installation:
        bundle = bundle_from_documents(documents)
        found = await self.core.installer.propose(OPERATOR, bundle, **options)
        await self.core.reconciler.sync(TENANT, NAME)
        return found

    async def agree(self, *, grants: Grants | None = None) -> Installation:
        found = await self.core.installer.consent(ADMIN, NAME, grants=grants)
        await self.core.reconciler.sync(TENANT, NAME)
        return found

    async def installed(self, documents: str | None = None, **options: Any) -> Installation:
        await self.offer(documents or _documents(), **options)
        return await self.agree()

    async def ask(self, message: str = "desk.note.read") -> RunHandle:
        return await self.runtime.chat(NAME, message, tenant=TENANT)

    async def facts(self) -> list[Event]:
        scope = Scope(TENANT, PLATFORM_APP_ID)
        return [stored.event async for stored in self.core.bus.replay(scope, "app.>")]


OpenDesk = Callable[[], Awaitable[Desk]]


async def _work(ctx: Context, task: dict[str, Any]) -> str:
    name = str(task.get("tool"))
    if name == "none":
        return "done"
    if name.startswith("memory:"):
        await ctx.memory.recall("anything", scopes=[name.split(":")[1]])  # type: ignore[list-item]
        return "read"
    if name.startswith("write:"):
        await ctx.memory.put("a note", scope=name.split(":")[1])  # type: ignore[arg-type]
        return "written"
    await ctx.tools.invoke(name)
    return "done"


@pytest.fixture
async def open_desk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[OpenDesk]:
    module = ModuleType("installer_handlers")
    module.work = _work  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "installer_handlers", module)
    running: list[LocalRuntime] = []

    async def make() -> Desk:
        desk_calls: list[str] = []

        @tool("desk.note.read", side_effect=SideEffect.READ, trusted=True)
        async def read() -> str:
            desk_calls.append("read")
            return "a note"

        @tool("desk.note.send", side_effect=SideEffect.WRITE, trusted=True)
        async def send() -> str:
            desk_calls.append("send")
            return "sent"

        runtime = LocalRuntime(tenant=TENANT)
        await runtime.__aenter__()
        running.append(runtime)
        await runtime.runtime.register_tool_provider(NativeProvider("desk", [read, send]))
        return Desk(runtime, desk_calls)

    yield make
    for runtime in running:
        await runtime.__aexit__(None, None, None)


# --------------------------------------------------------------------------- the consent step


async def test_an_app_doesnt_run_until_somebody_agrees(open_desk: OpenDesk) -> None:
    """M3 exit 4, first half."""
    desk = await open_desk()
    found = await desk.offer(_documents())
    assert (found.status, found.version, found.pending) == ("awaiting-consent", None, "1.0.0")
    with pytest.raises(NotFoundError):
        await desk.ask()


async def test_what_it_asks_for_is_on_the_record(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents(tools=("desk.note.read", "desk.note.send")))
    [asked] = await desk.facts()
    assert asked.type == "app.consent.requested"
    assert isinstance(asked.data, dict)
    requested = asked.data["requested"]
    assert isinstance(requested, dict)
    assert requested["tools"] == ["desk.note.read", "desk.note.send"]
    assert requested["memory_scopes"] == ["app", "user"]
    assert requested["hosts"] == ["docs.example.com", "wiki.example.com"]


async def test_agreeing_starts_it(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents())
    found = await desk.agree()
    assert (found.status, found.version, found.pending) == ("active", "1.0.0", None)
    assert found.granted_by == ADMIN.principal
    result = await (await desk.ask()).wait(timeout=10)
    assert result.status == "completed"
    assert desk.calls == ["read"]
    assert [event.type for event in await desk.facts()] == [
        "app.consent.requested",
        "app.installed",
    ]


async def test_refusing_starts_nothing(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents())
    found = await desk.core.installer.refuse(ADMIN, NAME)
    assert (found.status, found.pending) == ("uninstalled", None)
    with pytest.raises(ConflictError, match="waiting for consent"):
        await desk.agree()


async def test_nothing_to_agree_to_is_a_conflict(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    with pytest.raises(NotFoundError):
        await desk.agree()
    await desk.installed()
    with pytest.raises(ConflictError):
        await desk.agree()


# --------------------------------------------------------------------------- what wasn't granted


async def test_a_tool_left_out_is_blocked_at_runtime(open_desk: OpenDesk) -> None:
    """M3 exit 4, second half: asked for, not granted, and it can't be used."""
    desk = await open_desk()
    await desk.offer(_documents(tools=("desk.note.read", "desk.note.send")))
    await desk.agree(
        grants=Grants(
            tools=["desk.note.read"],
            data_classes=[DataClass.INTERNAL],
            memory_scopes=["app", "user"],
        )
    )
    allowed = await (await desk.ask("desk.note.read")).wait(timeout=10)
    assert allowed.status == "completed"
    refused = await (await desk.ask("desk.note.send")).wait(timeout=10)
    assert refused.status == "failed"
    assert desk.calls == ["read"]
    [denied] = [e for e in refused.events if e.type == "policy.decision.denied"]  # and no retry
    assert isinstance(denied.data, dict)
    assert denied.data["policies"] == ["consent/deny/0"]


async def test_granting_no_tools_at_all_blocks_every_tool(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents())
    await desk.agree(grants=Grants(data_classes=[DataClass.INTERNAL]))
    result = await (await desk.ask()).wait(timeout=10)
    assert result.status == "failed"
    assert desk.calls == []


async def test_a_pattern_grant_covers_its_tools(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents(tools=("desk.>",)))
    await desk.agree(
        grants=Grants(
            tools=["desk.note.send"],
            data_classes=[DataClass.INTERNAL],
            memory_scopes=["app", "user"],
        )
    )
    assert (await (await desk.ask("desk.note.send")).wait(timeout=10)).status == "completed"
    assert (await (await desk.ask("desk.note.read")).wait(timeout=10)).status == "failed"


async def test_an_apps_own_policy_cant_grant_itself_more(open_desk: OpenDesk) -> None:
    documents = _documents(tools=("desk.note.read", "desk.note.send")) + (
        "---\napiVersion: aios/v1alpha1\nkind: Policy\nmetadata: {name: wide, version: 1.0.0}\n"
        'spec:\n  subject: {app: desk}\n  allow:\n    - {action: tool.invoke, resource: "desk.>"}\n'
    )
    desk = await open_desk()
    await desk.offer(documents)
    await desk.agree(
        grants=Grants(
            tools=["desk.note.read"],
            data_classes=[DataClass.INTERNAL],
            memory_scopes=["app", "user"],
        )
    )
    refused = await (await desk.ask("desk.note.send")).wait(timeout=10)
    assert refused.status == "failed"
    assert desk.calls == []


async def test_a_memory_scope_left_out_is_blocked(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents())
    await desk.agree(
        grants=Grants(
            tools=["desk.note.read"], data_classes=[DataClass.INTERNAL], memory_scopes=["app"]
        )
    )
    assert (await (await desk.ask("memory:app")).wait(timeout=10)).status == "completed"
    assert (await (await desk.ask("memory:user")).wait(timeout=10)).status == "failed"


async def test_a_data_class_left_out_is_refused(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents())
    await desk.agree(grants=Grants(data_classes=[DataClass.INTERNAL]))
    with pytest.raises(PolicyDeniedError, match="confidential"):
        await desk.core.chat(NAME, "hello", tenant=TENANT, data_class=DataClass.CONFIDENTIAL)


async def test_a_host_left_out_is_off_the_egress_list(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents())
    await desk.agree(grants=Grants(data_classes=[DataClass.INTERNAL], hosts=["docs.example.com"]))
    egress = desk.core.tools.egress(TENANT, NAME, "desk.note.read")
    assert egress.permits("docs.example.com")
    assert not egress.permits("wiki.example.com")


async def test_an_agent_says_which_of_its_tools_were_refused(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents(tools=("desk.note.read", "desk.note.send")))
    await desk.agree(grants=Grants(tools=["desk.note.read"], data_classes=[DataClass.INTERNAL]))
    [agent] = await views.agents_of(desk.core, TENANT)
    assert agent.tools == ["desk.note.read", "desk.note.send"]
    assert agent.refused_tools == ["desk.note.send"]


async def test_an_app_started_with_a_process_refuses_nothing(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    desk.core.install(load_manifests(_documents(tools=("desk.note.read",))), tenant=TENANT)
    [agent] = await views.agents_of(desk.core, TENANT)
    assert agent.refused_tools == []


async def test_a_memory_scope_left_out_cant_be_written(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents())
    await desk.agree(grants=Grants(data_classes=[DataClass.INTERNAL], memory_scopes=["user"]))
    assert (await (await desk.ask("write:app")).wait(timeout=10)).status == "failed"


async def test_a_granted_memory_scope_can_be_written(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents())
    await desk.agree(grants=Grants(data_classes=[DataClass.INTERNAL], memory_scopes=["app"]))
    assert (await (await desk.ask("write:app")).wait(timeout=10)).status == "completed"


async def test_a_host_left_out_is_off_a_tools_own_list(open_desk: OpenDesk) -> None:
    own = '{"desk.note.read": [docs.example.com, wiki.example.com]}'
    documents = _documents().replace(
        "egress: {allow: [docs.example.com, wiki.example.com]}",
        f"egress: {{allow: [docs.example.com, wiki.example.com], tools: {own}}}",
    )
    desk = await open_desk()
    await desk.offer(documents)
    await desk.agree(grants=Grants(data_classes=[DataClass.INTERNAL], hosts=["docs.example.com"]))
    assert desk.core.tools.egress(TENANT, NAME, "desk.note.read").tool == ("docs.example.com",)


EVENTED = (
    "entrypoints: [{type: chat, workflow: ticket},"
    ' {type: event, on: "desk.ticket.opened", workflow: ticket}]'
)


def _evented() -> str:
    return _documents().replace("entrypoints: [{type: chat, workflow: ticket}]", EVENTED)


async def _opened(desk: Desk, data_class: DataClass = DataClass.INTERNAL) -> None:
    await desk.core.bus.publish(
        Event(
            source=f"aios://{TENANT}/apps/{NAME}/tickets",
            type="desk.ticket.opened",
            tenantid=TENANT,
            appid=NAME,
            principal="system:tickets",
            dataclass=data_class,
            data={"message": "none"},
        )
    )


async def _runs(desk: Desk) -> int:
    return len(await desk.core.runs(NAME, tenant=TENANT))


async def _until_runs(desk: Desk, count: int) -> None:
    async with asyncio.timeout(5):
        while await _runs(desk) < count:  # noqa: ASYNC110 - runs are read from the log
            await asyncio.sleep(0.01)


async def test_an_event_of_a_class_left_out_starts_nothing(open_desk: OpenDesk) -> None:
    """The engine's gate, for runs nobody asked for through the API."""
    desk = await open_desk()
    await desk.offer(_evented())
    await desk.agree(grants=Grants(data_classes=[DataClass.INTERNAL]))
    await _opened(desk, DataClass.CONFIDENTIAL)
    await _opened(desk, DataClass.INTERNAL)
    await _until_runs(desk, 1)
    await asyncio.sleep(0.1)
    assert await _runs(desk) == 1


async def test_granting_no_data_classes_starts_no_event_runs(open_desk: OpenDesk) -> None:
    """An empty grant is a refusal of everything, not the absence of a rule."""
    desk = await open_desk()
    await desk.offer(_evented())
    await desk.agree(grants=Grants())
    await _opened(desk)
    await asyncio.sleep(0.2)
    assert await _runs(desk) == 0


async def test_a_removed_app_takes_no_more_events(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_evented())
    await desk.agree()
    await _opened(desk)
    await _until_runs(desk, 1)
    await desk.core.uninstall(ADMIN, NAME, "retain")
    await _opened(desk)
    await asyncio.sleep(0.2)
    assert await _runs(desk) == 1
    engine = desk.core.engine
    assert engine is not None
    assert (TENANT, NAME) not in engine._watching  # pyright: ignore[reportPrivateUsage] - no leak


async def test_a_removed_app_leaves_nothing_behind(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.installed()
    await desk.core.uninstall(ADMIN, NAME, "retain")
    assert desk.core.catalog.get(TENANT, NAME) is None
    assert (TENANT, NAME) not in desk.core.enforcer._apps  # pyright: ignore[reportPrivateUsage]


async def test_a_change_of_grants_alone_reaches_the_running_app(open_desk: OpenDesk) -> None:
    """Same version, same configuration: only what was granted changed."""
    desk = await open_desk()
    await desk.offer(_documents(tools=(READ, SEND)))
    current = await desk.agree()
    narrower = current.grants.model_copy(update={"tools": [READ]})
    await desk.core.installations.save(current.model_copy(update={"grants": narrower}))
    await desk.core.reconciler.sync(TENANT, NAME)
    assert (await (await desk.ask(SEND)).wait(timeout=10)).status == "failed"
    assert desk.calls == []


async def test_an_offer_that_isnt_what_it_says_is_refused(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    bundle = bundle_from_documents(_documents())
    forged = bundle.model_copy(update={"documents": bundle.documents.replace("1.0.0", "1.0.1")})
    with pytest.raises(BundleError, match="digest"):
        await desk.core.installer.propose(OPERATOR, forged)
    assert await desk.core.installations.get(TENANT, NAME) is None


async def test_nobody_grants_what_wasnt_asked_for(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents())
    with pytest.raises(BundleError, match="didn't ask for that"):
        await desk.agree(grants=Grants(tools=["stripe.refund.create"]))
    with pytest.raises(BundleError, match="didn't ask for that"):
        await desk.agree(grants=Grants(data_classes=[DataClass.RESTRICTED]))


# --------------------------------------------------------------------------- upgrades


async def test_an_upgrade_that_asks_for_nothing_new_goes_straight_in(
    open_desk: OpenDesk,
) -> None:
    desk = await open_desk()
    await desk.installed()
    found = await desk.offer(_documents("2.0.0"))
    assert (found.version, found.pending) == ("2.0.0", None)
    assert desk.core.app(NAME, tenant=TENANT).manifest.metadata.version == "2.0.0"
    assert (await desk.facts())[-1].type == "app.upgraded"


async def test_an_upgrade_that_asks_for_more_waits_and_the_old_one_keeps_running(
    open_desk: OpenDesk,
) -> None:
    desk = await open_desk()
    await desk.installed()
    found = await desk.offer(_documents("2.0.0", tools=("desk.note.read", "desk.note.send")))
    assert (found.status, found.version, found.pending) == ("active", "1.0.0", "2.0.0")
    assert desk.core.app(NAME, tenant=TENANT).manifest.metadata.version == "1.0.0"
    assert (await (await desk.ask()).wait(timeout=10)).status == "completed"

    found = await desk.agree()
    assert (found.version, found.pending) == ("2.0.0", None)
    assert desk.core.app(NAME, tenant=TENANT).manifest.metadata.version == "2.0.0"


async def test_refusing_an_upgrade_leaves_the_old_one_running(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.installed()
    await desk.offer(_documents("2.0.0", tools=("desk.note.read", "desk.note.send")))
    found = await desk.core.installer.refuse(ADMIN, NAME)
    assert (found.status, found.version, found.pending) == ("active", "1.0.0", None)
    assert (await (await desk.ask()).wait(timeout=10)).status == "completed"


async def test_an_upgrade_keeps_what_was_granted(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents(tools=("desk.note.read",)))
    await desk.agree(
        grants=Grants(data_classes=[DataClass.INTERNAL], memory_scopes=["app", "user"])
    )
    await desk.offer(_documents("2.0.0", tools=("desk.note.read",)))
    assert (await (await desk.ask()).wait(timeout=10)).status == "failed"


async def test_a_version_never_changes_its_contents(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.installed()
    with pytest.raises(ConflictError, match="never changes"):
        await desk.offer(_documents(tools=("desk.note.send",)))


async def test_offering_what_is_running_changes_nothing(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.installed()
    before = await desk.facts()
    found = await desk.offer(_documents())
    assert found.version == "1.0.0"
    assert await desk.facts() == before


async def test_a_run_finishes_on_the_version_it_started_on(open_desk: OpenDesk) -> None:
    """FR-APP-04. The run is paused while the App changes under it."""
    desk = await open_desk()
    await desk.installed(_pausing("1.0.0", "old-step"))
    run = await desk.ask()
    asked = await run.wait_for_event("approval.requested", timeout=10)
    await desk.offer(_pausing("2.0.0", "new-step"))
    assert isinstance(asked.data, dict)
    await desk.runtime.approve(str(asked.data["approval"]), tenant=TENANT)
    result = await run.wait(timeout=10)
    assert result.status == "completed"
    steps = {e.data["step"] for e in result.events if e.type == "step.completed"}  # type: ignore[index]
    assert steps == {"ask", "old-step"}


async def test_a_new_run_uses_the_new_version(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.installed(_pausing("1.0.0", "old-step"))
    await desk.offer(_pausing("2.0.0", "new-step"))
    run = await desk.ask()
    asked = await run.wait_for_event("approval.requested", timeout=10)
    assert isinstance(asked.data, dict)
    await desk.runtime.approve(str(asked.data["approval"]), tenant=TENANT)
    result = await run.wait(timeout=10)
    steps = {e.data["step"] for e in result.events if e.type == "step.completed"}  # type: ignore[index]
    assert steps == {"ask", "new-step"}


async def test_agents_keep_working_across_an_upgrade(open_desk: OpenDesk) -> None:
    """The worker is replaced, not joined by a second one competing for its work."""
    desk = await open_desk()
    await desk.installed()
    await desk.offer(_documents("2.0.0"))
    await desk.offer(_documents("3.0.0"))
    serving = [w for w in desk.core.workers if w.serves(TENANT, NAME)]
    assert len(serving) == 1
    assert (await (await desk.ask()).wait(timeout=10)).status == "completed"
    assert desk.calls == ["read"]


# --------------------------------------------------------------------------- going back


async def test_a_rollback_runs_the_older_version(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.installed()
    await desk.offer(_documents("2.0.0"))
    found = await desk.core.installer.rollback(ADMIN, NAME, "1.0.0")
    await desk.core.reconciler.sync(TENANT, NAME)
    assert found.version == "1.0.0"
    assert desk.core.app(NAME, tenant=TENANT).manifest.metadata.version == "1.0.0"
    assert (await desk.facts())[-1].type == "app.version.restored"


async def test_a_rollback_needs_a_version_that_exists(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.installed()
    with pytest.raises(NotFoundError, match="no version"):
        await desk.core.installer.rollback(ADMIN, NAME, "9.9.9")


async def test_a_rollback_that_asks_for_more_waits(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents(tools=("desk.note.read", "desk.note.send")))
    await desk.agree()
    await desk.offer(_documents("2.0.0"))
    await desk.core.installer.uninstall(ADMIN, NAME, "retain")
    await desk.offer(_documents("2.0.0"))
    await desk.agree()
    found = await desk.core.installer.rollback(ADMIN, NAME, "1.0.0")
    assert (found.version, found.pending) == ("2.0.0", "1.0.0")


# --------------------------------------------------------------------------- what's remembered


READ, SEND = "desk.note.read", "desk.note.send"
NO_SEND = Grants(
    tools=[READ],
    data_classes=[DataClass.INTERNAL, DataClass.CONFIDENTIAL],
    memory_scopes=["app", "user"],
    hosts=["docs.example.com", "wiki.example.com"],
)


async def _refused_send(desk: Desk) -> None:
    await desk.offer(_documents(tools=(READ, SEND)))
    await desk.agree(grants=NO_SEND)


async def test_a_refusal_is_remembered(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await _refused_send(desk)
    found = await desk.core.installations.get(TENANT, NAME)
    assert found is not None
    assert found.refused.tools == [SEND]


async def test_a_refusal_outlives_an_upgrade(open_desk: OpenDesk) -> None:
    """Agreeing to what's new isn't agreeing again to what was refused."""
    desk = await open_desk()
    await _refused_send(desk)
    extra = "desk.note.archive"
    await desk.offer(_documents("2.0.0", tools=(READ, SEND, extra)))
    upgraded = await desk.agree()
    assert upgraded.grants.tools == [extra, READ]
    assert upgraded.refused.tools == [SEND]
    assert (await (await desk.ask(SEND)).wait(timeout=10)).status == "failed"
    assert "send" not in desk.calls


async def test_a_version_that_asks_nothing_new_doesnt_ask(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await _refused_send(desk)
    found = await desk.offer(_documents("2.0.0", tools=(READ, SEND)))
    assert (found.version, found.pending) == ("2.0.0", None)
    assert found.grants.tools == [READ]
    assert found.refused.tools == [SEND]


async def test_taking_a_refusal_back_takes_saying_so(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await _refused_send(desk)
    await desk.offer(_documents("2.0.0", tools=(READ, SEND, "desk.note.archive")))
    everything = NO_SEND.model_copy(update={"tools": [READ, SEND, "desk.note.archive"]})
    upgraded = await desk.agree(grants=everything)
    assert upgraded.grants.tools == [READ, SEND, "desk.note.archive"]
    assert upgraded.refused.tools == []


async def test_configuring_doesnt_reopen_a_refusal(open_desk: OpenDesk, tmp_path: Path) -> None:
    """It used to park the change behind a consent request nobody had reason to expect."""
    desk = await open_desk()
    await desk.core.installer.propose(OPERATOR, _configured(tmp_path))
    found = await desk.agree(grants=Grants())
    assert found.refused.tools == ["desk.note.read"]
    changed = await desk.core.installer.configure(ADMIN, NAME, {"tool": "none"})
    assert (changed.config, changed.pending) == ({"tool": "none"}, None)


async def test_an_upgrade_keeps_the_configuration(open_desk: OpenDesk, tmp_path: Path) -> None:
    desk = await open_desk()
    await desk.core.installer.propose(OPERATOR, _configured(tmp_path / "one"))
    await desk.agree()
    await desk.core.installer.configure(ADMIN, NAME, {"tool": "none"})
    newer = _configured(tmp_path / "two", version="2.0.0")
    found = await desk.core.installer.propose(OPERATOR, newer)
    assert (found.version, found.config) == ("2.0.0", {"tool": "none"})


async def test_an_upgrade_can_be_given_new_configuration(
    open_desk: OpenDesk, tmp_path: Path
) -> None:
    desk = await open_desk()
    await desk.core.installer.propose(OPERATOR, _configured(tmp_path / "one"))
    await desk.agree()
    newer = _configured(tmp_path / "two", version="2.0.0")
    found = await desk.core.installer.propose(OPERATOR, newer, config={"tool": "none"})
    assert found.config == {"tool": "none"}


async def test_a_rollback_gets_no_more_than_it_asked_for(open_desk: OpenDesk) -> None:
    """What the newer version was given doesn't come back with the older one."""
    desk = await open_desk()
    await desk.offer(_documents(tools=(READ,)))
    await desk.agree()
    extra = "desk.note.archive"
    await desk.offer(_documents("2.0.0", tools=(READ, extra)))
    assert extra in (await desk.agree()).grants.tools
    back = await desk.core.installer.rollback(ADMIN, NAME, "1.0.0")
    assert back.grants.tools == [READ]


async def test_going_forward_again_asks_again(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.offer(_documents(tools=(READ,)))
    await desk.agree()
    await desk.offer(_documents("2.0.0", tools=(READ, "desk.note.archive")))
    await desk.agree()
    await desk.core.installer.rollback(ADMIN, NAME, "1.0.0")
    found = await desk.offer(_documents("2.0.0", tools=(READ, "desk.note.archive")))
    assert (found.version, found.pending) == ("1.0.0", "2.0.0")


async def test_uninstalling_forgets_the_answers(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await _refused_send(desk)
    removed = await desk.core.installer.uninstall(ADMIN, NAME, "retain")
    assert (removed.grants, removed.refused) == (Grants(), Grants())
    found = await desk.offer(_documents(tools=(READ, SEND)))
    assert found.refused.tools == []
    assert (await desk.agree()).grants.tools == [READ, SEND]


# --------------------------------------------------------------------------- configuration


CONFIGURED = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: %s}
spec:
  entrypoints: [{type: chat, workflow: ticket}]
  agents: [worker]
  workflows: [ticket]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: worker, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "installer_handlers:work"}
  capabilities: [{name: desk.work}]
  tools: {allow: ["desk.note.read"]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: ticket, version: 1.0.0}
spec:
  steps:
    - {id: first, type: task, capability: desk.work, input: {tool: "${config.tool}"}}
"""


SCHEMA = """{"type": "object", "additionalProperties": false,
"properties": {"tool": {"type": "string", "default": "desk.note.read"}}}"""


def _configured(root: Path, *, version: str = "1.0.0", documents: str = "") -> Bundle:
    (root / "manifests").mkdir(parents=True)
    (root / "manifests" / "app.yaml").write_text((documents or CONFIGURED) % version, "utf-8")
    (root / "config.schema.json").write_text(SCHEMA, "utf-8")
    return bundle_of(root)


async def test_configuration_is_checked_before_anything_changes(
    open_desk: OpenDesk, tmp_path: Path
) -> None:
    desk = await open_desk()
    await desk.core.installer.propose(OPERATOR, _configured(tmp_path))
    await desk.agree()
    with pytest.raises(BundleError, match="invalid configuration"):
        await desk.core.installer.configure(ADMIN, NAME, {"nonsense": 1})
    found = await desk.core.installations.get(TENANT, NAME)
    assert found is not None
    assert found.config == {"tool": "desk.note.read"}


async def test_configuration_left_out_takes_its_default(
    open_desk: OpenDesk, tmp_path: Path
) -> None:
    desk = await open_desk()
    await desk.core.installer.propose(OPERATOR, _configured(tmp_path), config={"tool": "none"})
    await desk.agree()
    found = await desk.core.installer.configure(ADMIN, NAME, {})
    assert found.config == {"tool": "desk.note.read"}


async def test_configuration_that_asks_for_more_needs_consent(
    open_desk: OpenDesk, tmp_path: Path
) -> None:
    """A tool named by configuration is still a tool the tenant has to agree to."""
    documents = CONFIGURED.replace(
        'tools: {allow: ["desk.note.read"]}', 'tools: {allow: ["${config.tool}"]}'
    )
    desk = await open_desk()
    await desk.core.installer.propose(OPERATOR, _configured(tmp_path, documents=documents))
    await desk.agree()
    found = await desk.core.installer.configure(ADMIN, NAME, {"tool": "desk.note.send"})
    assert (found.config, found.pending) == ({"tool": "desk.note.read"}, "1.0.0")
    assert found.pending_config == {"tool": "desk.note.send"}


async def test_configuration_reaches_the_running_app(open_desk: OpenDesk, tmp_path: Path) -> None:
    desk = await open_desk()
    await desk.core.installer.propose(OPERATOR, _configured(tmp_path))
    await desk.agree()
    await desk.core.installer.configure(ADMIN, NAME, {"tool": "none"})
    await desk.core.reconciler.sync(TENANT, NAME)
    assert (await (await desk.ask()).wait(timeout=10)).status == "completed"
    assert desk.calls == []
    assert (await desk.facts())[-1].type == "app.config.changed"


# --------------------------------------------------------------------------- uninstalling


async def _with_knowledge(desk: Desk) -> None:
    await desk.installed()
    folder = Path(__file__).parents[2] / "fixtures" / "kb"
    await desk.runtime.ingest_knowledge(NAME, folder, tenant=TENANT)


async def test_uninstalling_can_keep_the_data(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await _with_knowledge(desk)
    found = await desk.core.uninstall(ADMIN, NAME, "retain")
    assert (found.status, found.version) == ("uninstalled", None)
    assert (TENANT, NAME) not in desk.core.apps
    assert await desk.core.memory.of_app(TENANT, NAME) != []


async def test_uninstalling_can_delete_the_data(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await _with_knowledge(desk)
    await desk.core.uninstall(ADMIN, NAME, "delete")
    assert await desk.core.memory.of_app(TENANT, NAME) == []


async def test_uninstalling_leaves_a_persons_own_memory_alone(open_desk: OpenDesk) -> None:
    """It belongs to the person, not the App."""
    desk = await open_desk()
    await desk.installed()
    now = datetime.now(UTC)
    alice = "user:alice@acme"
    item = MemoryItem(
        id=new_ulid(),
        tenant=TENANT,
        scope="user",
        owner=alice,
        content="prefers email",
        provenance=Provenance(author=alice, created_at=now),
        updated_at=now,
    )
    await desk.core.memory.store.add([Stored(item)])
    await desk.core.uninstall(ADMIN, NAME, "delete")
    assert await desk.core.memory.store.get(TENANT, item.id) == item


async def test_uninstalling_stops_what_is_running(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.installed(_pausing("1.0.0", "last"))
    run = await desk.ask()
    await run.wait_for_event("approval.requested", timeout=10)
    await desk.core.uninstall(ADMIN, NAME, "retain")
    assert (await run.wait(timeout=10)).status == "cancelled"


async def test_an_uninstalled_app_stops_serving(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.installed()
    await desk.core.uninstall(ADMIN, NAME, "retain")
    assert not [w for w in desk.core.workers if w.serves(TENANT, NAME)]
    with pytest.raises(NotFoundError):
        await desk.ask()
    assert (await desk.facts())[-1].type == "app.uninstalled"


async def test_coming_back_asks_again(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    await desk.installed()
    await desk.core.uninstall(ADMIN, NAME, "retain")
    found = await desk.offer(_documents("1.1.0"))
    assert (found.status, found.pending) == ("awaiting-consent", "1.1.0")


# --------------------------------------------------------------------------- every process


async def test_a_second_process_follows(tmp_path: Path) -> None:
    """What makes the record the truth: nobody tells this process anything directly."""
    module = ModuleType("installer_handlers")
    module.work = _work  # type: ignore[attr-defined]
    sys.modules["installer_handlers"] = module
    async with InMemoryEventBus() as bus:
        store = InMemoryInstallationStore(bus)
        api = Runtime(bus, tenant=TENANT, installations=store, sandbox=tmp_path)
        worker = Runtime(bus, tenant=TENANT, installations=store, sandbox=tmp_path)
        await api.reconciler.start()
        await worker.start()
        try:
            bundle = bundle_from_documents(_documents())
            await api.installer.propose(OPERATOR, bundle)
            await api.installer.consent(ADMIN, NAME)
            await _until(lambda: (TENANT, NAME) in worker.apps)
            await api.installer.uninstall(ADMIN, NAME, "retain")
            await _until(lambda: (TENANT, NAME) not in worker.apps)
        finally:
            await worker.stop()
            await api.reconciler.stop()


async def _until(done: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not done():  # noqa: ASYNC110 - the reconciler has no event to wait on
            await asyncio.sleep(0.01)


async def test_a_new_process_finds_what_is_installed(open_desk: OpenDesk, tmp_path: Path) -> None:
    desk = await open_desk()
    await desk.installed()
    fresh = Runtime(
        desk.core.bus, tenant=TENANT, installations=desk.core.installations, sandbox=tmp_path
    )
    await fresh.reconciler.start()
    try:
        assert (TENANT, NAME) in fresh.apps
    finally:
        await fresh.reconciler.stop()


async def test_a_new_process_finishes_runs_on_older_versions(
    open_desk: OpenDesk, tmp_path: Path
) -> None:
    """Every stored version's workflows are loaded, or a restart would strand a paused run."""
    desk = await open_desk()
    await desk.installed(_pausing("1.0.0", "old-step"))
    await desk.offer(_pausing("2.0.0", "new-step"))
    fresh = Runtime(
        desk.core.bus, tenant=TENANT, installations=desk.core.installations, sandbox=tmp_path
    )
    await fresh.reconciler.start()
    try:
        definition = fresh.catalog.get(TENANT, NAME)
        assert definition is not None
        assert {version for _, version in definition.versions} == {"1.0.0", "2.0.0"}
    finally:
        await fresh.reconciler.stop()


async def test_a_version_that_wont_load_is_refused_when_offered(open_desk: OpenDesk) -> None:
    """Otherwise it would become what every process keeps failing to run."""
    desk = await open_desk()
    await desk.installed()
    broken = _documents("2.0.0").replace("installer_handlers:work", "nowhere_at_all:work")
    with pytest.raises(BundleError, match="won't load"):
        await desk.offer(broken)
    found = await desk.core.installations.get(TENANT, NAME)
    assert found is not None
    assert (found.version, found.pending) == ("1.0.0", None)
    assert await desk.core.installations.version(TENANT, NAME, "2.0.0") is None
    assert (await (await desk.ask()).wait(timeout=10)).status == "completed"


async def test_one_broken_app_doesnt_stop_the_others(open_desk: OpenDesk) -> None:
    """Code can go missing after it was offered, and this process still has others to run."""
    desk = await open_desk()
    await desk.installed()
    broken = bundle_from_documents(
        _documents().replace("desk", "other").replace("installer_handlers:work", "gone:work")
    )
    store = desk.core.installations
    await store.add_version(TENANT, broken)
    record = Installation(
        tenant=TENANT,
        app="other",
        status="active",
        version=broken.version,
        grants=Grants(),
        updated_at=datetime.now(UTC),
    )
    await store.save(record)
    await desk.core.reconciler.sync_everything()
    assert (TENANT, "other") not in desk.core.apps
    assert (await (await desk.ask()).wait(timeout=10)).status == "completed"


async def test_an_app_given_to_a_process_is_left_alone(open_desk: OpenDesk) -> None:
    desk = await open_desk()
    desk.core.install(load_manifests(_documents()), tenant=TENANT)
    await desk.core.reconciler.sync_everything()
    assert (TENANT, NAME) in desk.core.apps

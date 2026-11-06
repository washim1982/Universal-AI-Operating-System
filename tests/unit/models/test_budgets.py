"""Budgets (FR-MOD-08, ADR-018): a hard limit denies a call, a soft one warns and can move it."""

import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import pytest

from aios.core.catalog import (
    BudgetExceeded,
    BudgetLevel,
    BudgetWarning,
    ModelCallCompleted,
    ModelCallFailed,
    RunFailed,
)
from aios.core.envelope import DataClass, Event
from aios.core.errors import BudgetExceededError
from aios.core.manifests import (
    AppBudgets,
    ModelPolicyManifest,
    load_manifests,
    parse_manifest,
)
from aios.core.ports import CallSite, EmbeddingRequest, Message, ModelRequest
from aios.fabric import InMemoryEventBus, Scope
from aios.kernel import Context
from aios.models import (
    Completion,
    Embedding,
    ModelEntry,
    ModelKernel,
    ModelRegistry,
    Prices,
    Provider,
    ProviderError,
    Router,
)
from aios.models.ledger import subjects_of
from aios.observability import EventMetrics
from aios.testing import task_request
from aios.testing.runtime import LocalRuntime

SCOPE = Scope("acme", "ai-researcher")
USER = "user:alice@acme"
DEAR = ModelEntry(id="fake/dear", model="dear", prices=Prices(input=10.0, output=10.0))
CHEAP = ModelEntry(id="fake/cheap", model="cheap", prices=Prices(input=1.0, output=1.0))
PRIVATE = ModelEntry(id="fake/ours", model="ours", deployment="private")
CALL_COST = 0.02  # 1,000 + 1,000 tokens of DEAR


class Fake(Provider):
    name = "fake"

    def __init__(self, *errors: ProviderError) -> None:
        self.calls: list[str] = []
        self.errors = list(errors)

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        self.calls.append(entry.id)
        if self.errors:
            raise self.errors.pop(0)
        return Completion(text="ok", input_tokens=1_000, output_tokens=1_000, model=entry.model)

    async def embed(self, entry: ModelEntry, request: EmbeddingRequest) -> Embedding:
        self.calls.append(entry.id)
        return Embedding(vectors=[[1.0]], input_tokens=1_000, model=entry.model)


def _policy(budgets: dict[str, Any], *rules: dict[str, Any]) -> ModelPolicyManifest:
    manifest = parse_manifest(
        {
            "apiVersion": "aios/v1alpha1",
            "kind": "ModelPolicy",
            "metadata": {"name": "default", "version": "1.0.0"},
            "spec": {
                "rules": list(rules),
                "default": {"candidates": [DEAR.id]},
                "budgets": budgets,
            },
        }
    )
    assert isinstance(manifest, ModelPolicyManifest)
    return manifest


SOFT_RULE = {"match": {"budget": "soft"}, "candidates": [CHEAP.id]}


def _kernel(bus: InMemoryEventBus, policy: ModelPolicyManifest, provider: Fake) -> ModelKernel:
    return ModelKernel(
        bus,
        registry=ModelRegistry([DEAR, CHEAP, PRIVATE]),
        policies={"default": policy},
        providers={"fake": provider},
    )


def _site(**fields: Any) -> CallSite:
    event = task_request("planning", step="plan", onbehalfof=USER, **fields)
    return CallSite(
        event=event,
        agent="planner-agent",
        principal="agent:planner-agent@acme",
        index=0,
        step="plan",
    )


def _request(**fields: Any) -> ModelRequest:
    return ModelRequest(task="planning", messages=[Message(role="user", content="hi")], **fields)


async def _spend(kernel: ModelKernel, site: CallSite, level: BudgetLevel, usd: float) -> None:
    paying = subjects_of(site, datetime.now(UTC))
    await kernel.ledger.charge(site.event.tenantid, [paying[level]], usd, _noise(site))


def _noise(site: CallSite) -> Event:
    return site.event.derive(
        "budget.warning",
        BudgetWarning(
            level="run", limit=1.0, soft_limit=1.0, observed=0.0, action="warn"
        ).model_dump(mode="json"),
        source="aios://acme/system/test",
        principal="system:test",
    )


async def _of_type(bus: InMemoryEventBus, type: str) -> list[Event]:
    return [s.event async for s in bus.replay(SCOPE, type)]


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus() as bus:
        yield bus


# --------------------------------------------------------------------------- hard limits


@pytest.mark.parametrize(
    ("level", "budgets"),
    [
        ("run", {"perRunUSD": 1.0}),
        ("user", {"perUserMonthlyUSD": 1.0}),
        ("app", {"perAppMonthlyUSD": 1.0}),
        ("tenant", {"perTenantMonthlyUSD": 1.0}),
    ],
)
async def test_a_hard_limit_denies_the_call(
    bus: InMemoryEventBus, level: BudgetLevel, budgets: dict[str, Any]
) -> None:
    provider = Fake()
    kernel = _kernel(bus, _policy(budgets), provider)
    site = _site()
    await _spend(kernel, site, level, 1.0)
    with pytest.raises(BudgetExceededError) as raised:
        await kernel.generate(_request(), site)
    assert (raised.value.budget, raised.value.level) == ("cost", level)
    assert (raised.value.limit, raised.value.observed) == (1.0, 1.0)
    assert provider.calls == []
    [failed] = await _of_type(bus, "model.call.failed")
    error = ModelCallFailed.model_validate(failed.data).error
    assert (error.code, error.retryable) == ("budget.exceeded", False)


async def test_under_a_limit_the_call_goes_through(bus: InMemoryEventBus) -> None:
    provider = Fake()
    kernel = _kernel(bus, _policy({"perRunUSD": 1.0}), provider)
    site = _site()
    await _spend(kernel, site, "run", 0.5)
    await kernel.generate(_request(), site)
    assert provider.calls == [DEAR.id]
    assert await _of_type(bus, "model.call.failed") == []


async def test_no_budgets_means_no_limit(bus: InMemoryEventBus) -> None:
    provider = Fake()
    kernel = _kernel(bus, _policy({}), provider)
    site = _site()
    await _spend(kernel, site, "tenant", 1_000_000.0)
    await kernel.generate(_request(), site)
    assert provider.calls == [DEAR.id]


async def test_a_run_limit_is_for_that_run(bus: InMemoryEventBus) -> None:
    provider = Fake()
    kernel = _kernel(bus, _policy({"perRunUSD": 1.0}), provider)
    spent, other = _site(), _site()
    await _spend(kernel, spent, "run", 1.0)
    await kernel.generate(_request(), other)
    assert provider.calls == [DEAR.id]


async def test_a_user_limit_is_for_that_user(bus: InMemoryEventBus) -> None:
    provider = Fake()
    kernel = _kernel(bus, _policy({"perUserMonthlyUSD": 1.0}), provider)
    await _spend(kernel, _site(), "user", 1.0)
    event = task_request("planning", step="plan", onbehalfof="user:bob@acme")
    other = CallSite(event=event, agent="planner-agent", principal="agent:x@acme", index=0)
    await kernel.generate(_request(), other)
    assert provider.calls == [DEAR.id]


async def test_embeddings_are_held_to_it_too(bus: InMemoryEventBus) -> None:
    provider = Fake()
    kernel = _kernel(bus, _policy({"perRunUSD": 1.0}), provider)
    site = _site()
    await _spend(kernel, site, "run", 1.0)
    request = EmbeddingRequest(task="embedding", texts=["hi"])
    with pytest.raises(BudgetExceededError):
        await kernel.embed(request, site)
    assert provider.calls == []


@pytest.mark.parametrize(
    ("app", "policy", "limit"),
    [
        (AppBudgets(perRunUSD=1.0), {"perRunUSD": 2.0}, 1.0),
        (AppBudgets(perRunUSD=2.0), {"perRunUSD": 1.0}, 1.0),
        (AppBudgets(perRunUSD=1.0), {}, 1.0),
    ],
)
async def test_the_tighter_of_an_app_and_its_policy_holds(
    bus: InMemoryEventBus, app: AppBudgets, policy: dict[str, Any], limit: float
) -> None:
    kernel = _kernel(bus, _policy(policy), Fake())
    site = _site()
    kernel.use_budgets(site.event.tenantid, site.event.appid, app)
    await _spend(kernel, site, "run", limit)
    with pytest.raises(BudgetExceededError) as raised:
        await kernel.generate(_request(), site)
    assert raised.value.limit == limit


async def test_an_apps_monthly_limit_counts(bus: InMemoryEventBus) -> None:
    kernel = _kernel(bus, _policy({}), Fake())
    site = _site()
    kernel.use_budgets(site.event.tenantid, site.event.appid, AppBudgets(monthlyUSD=1.0))
    await _spend(kernel, site, "app", 1.0)
    with pytest.raises(BudgetExceededError) as raised:
        await kernel.generate(_request(), site)
    assert raised.value.level == "app"


def test_a_month_starts_again_but_a_run_doesnt() -> None:
    site = _site()
    october = subjects_of(site, datetime(2026, 10, 31, 23, 59, tzinfo=UTC))
    november = subjects_of(site, datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert october["tenant"] != november["tenant"]
    assert october["app"] != november["app"]
    assert october["user"] != november["user"]
    assert october["run"] == november["run"]


# --------------------------------------------------------------------------- soft limits


async def test_a_soft_limit_moves_the_route(bus: InMemoryEventBus) -> None:
    provider = Fake()
    budgets = {"perTenantMonthlyUSD": 1.0, "onSoftLimit": "downgrade"}
    kernel = _kernel(bus, _policy(budgets, SOFT_RULE), provider)
    site = _site()
    await _spend(kernel, site, "tenant", 0.8)
    response = await kernel.generate(_request(), site)
    assert provider.calls == [CHEAP.id]
    assert response.model == "cheap"
    [done] = await _of_type(bus, "model.call.completed")
    assert ModelCallCompleted.model_validate(done.data).downgraded is True


async def test_below_a_soft_limit_nothing_changes(bus: InMemoryEventBus) -> None:
    provider = Fake()
    budgets = {"perTenantMonthlyUSD": 1.0, "onSoftLimit": "downgrade"}
    kernel = _kernel(bus, _policy(budgets, SOFT_RULE), provider)
    site = _site()
    await _spend(kernel, site, "tenant", 0.79)
    await kernel.generate(_request(), site)
    assert provider.calls == [DEAR.id]
    assert [e for e in await _of_type(bus, "budget.warning") if e.data != _noise(site).data] == []
    [done] = await _of_type(bus, "model.call.completed")
    assert ModelCallCompleted.model_validate(done.data).downgraded is False


async def test_soft_can_be_set(bus: InMemoryEventBus) -> None:
    provider = Fake()
    budgets = {"perRunUSD": 1.0, "softAt": 0.5, "onSoftLimit": "downgrade"}
    kernel = _kernel(bus, _policy(budgets, SOFT_RULE), provider)
    site = _site()
    await _spend(kernel, site, "run", 0.5)
    await kernel.generate(_request(), site)
    assert provider.calls == [CHEAP.id]


async def test_a_soft_limit_warns_once(bus: InMemoryEventBus) -> None:
    budgets = {"perTenantMonthlyUSD": 1.0, "onSoftLimit": "downgrade"}
    kernel = _kernel(bus, _policy(budgets, SOFT_RULE), Fake())
    first, second = _site(), _site()
    await _spend(kernel, first, "tenant", 0.8)
    before = len(await _of_type(bus, "budget.warning"))
    await kernel.generate(_request(), first)
    await kernel.generate(_request(), second)
    warnings = (await _of_type(bus, "budget.warning"))[before:]
    assert len(warnings) == 1
    warning = BudgetWarning.model_validate(warnings[0].data)
    assert warning.level == "tenant"
    assert warning.limit == 1.0
    assert warning.soft_limit == 0.8
    assert warning.action == "downgrade"


async def test_two_kernels_warn_once_between_them(bus: InMemoryEventBus) -> None:
    """A second worker has its own memory of what it warned about, but the ledger is shared."""
    budgets = {"perTenantMonthlyUSD": 1.0}
    first = _kernel(bus, _policy(budgets), Fake())
    second = _kernel(bus, _policy(budgets), Fake())
    second.ledger = first.ledger
    site = _site()
    await _spend(first, site, "tenant", 0.8)
    before = len(await _of_type(bus, "budget.warning"))
    await first.generate(_request(), site)
    await second.generate(_request(), replace(site, index=1))
    assert len(await _of_type(bus, "budget.warning")) == before + 1


async def test_warn_only_warns(bus: InMemoryEventBus) -> None:
    provider = Fake()
    kernel = _kernel(bus, _policy({"perTenantMonthlyUSD": 1.0, "onSoftLimit": "warn"}), provider)
    site = _site()
    await _spend(kernel, site, "tenant", 0.8)
    before = len(await _of_type(bus, "budget.warning"))
    await kernel.generate(_request(), site)
    assert provider.calls == [DEAR.id]
    assert len(await _of_type(bus, "budget.warning")) == before + 1


async def test_warn_doesnt_use_a_soft_rule(bus: InMemoryEventBus) -> None:
    provider = Fake()
    budgets = {"perTenantMonthlyUSD": 1.0, "onSoftLimit": "warn"}
    kernel = _kernel(bus, _policy(budgets, SOFT_RULE), provider)
    site = _site()
    await _spend(kernel, site, "tenant", 0.8)
    await kernel.generate(_request(), site)
    assert provider.calls == [DEAR.id]


async def test_deny_stops_at_the_soft_limit(bus: InMemoryEventBus) -> None:
    provider = Fake()
    kernel = _kernel(bus, _policy({"perTenantMonthlyUSD": 1.0, "onSoftLimit": "deny"}), provider)
    site = _site()
    await _spend(kernel, site, "tenant", 0.8)
    with pytest.raises(BudgetExceededError) as raised:
        await kernel.generate(_request(), site)
    assert raised.value.limit == pytest.approx(0.8)
    assert provider.calls == []


async def test_a_hard_limit_wins_over_a_soft_one(bus: InMemoryEventBus) -> None:
    budgets = {"perTenantMonthlyUSD": 1.0, "perRunUSD": 100.0, "onSoftLimit": "downgrade"}
    provider = Fake()
    kernel = _kernel(bus, _policy(budgets, SOFT_RULE), provider)
    site = _site()
    await _spend(kernel, site, "run", 90.0)  # soft, and first in line
    await _spend(kernel, site, "tenant", 1.0)  # hard
    with pytest.raises(BudgetExceededError) as raised:
        await kernel.generate(_request(), site)
    assert raised.value.level == "tenant"


async def test_a_downgrade_doesnt_get_around_the_data_class_guard(bus: InMemoryEventBus) -> None:
    provider = Fake()
    budgets = {"perTenantMonthlyUSD": 1.0, "onSoftLimit": "downgrade"}
    rule = {"match": {"dataClass": "restricted"}, "candidates": [PRIVATE.id]}
    kernel = _kernel(bus, _policy(budgets, SOFT_RULE, rule), provider)
    site = _site()
    await _spend(kernel, site, "tenant", 0.8)
    response = await kernel.generate(_request(data_class=DataClass.RESTRICTED), site)
    assert provider.calls == [PRIVATE.id]  # the soft rule's only model is public
    assert response.model == "ours"


async def test_nowhere_to_go_keeps_the_route(bus: InMemoryEventBus) -> None:
    """A soft limit changes which model answers. It never stops the work by itself."""
    provider = Fake()
    budgets = {"perTenantMonthlyUSD": 1.0, "onSoftLimit": "downgrade"}
    rule = {"match": {"budget": "soft"}, "candidates": ["fake/unknown"]}
    kernel = _kernel(bus, _policy(budgets, rule), provider)
    site = _site()
    await _spend(kernel, site, "tenant", 0.8)
    await kernel.generate(_request(), site)
    assert provider.calls == [DEAR.id]
    [done] = await _of_type(bus, "model.call.completed")
    assert ModelCallCompleted.model_validate(done.data).downgraded is False


def test_a_soft_rule_can_be_narrowed() -> None:
    router = Router(ModelRegistry([DEAR, CHEAP]))
    rule = {"match": {"budget": "soft", "task": "summary"}, "candidates": [CHEAP.id]}
    policy = _policy({}, rule)
    route = router.route(policy, _request(), soft=True)
    assert [entry.id for entry in route.candidates] == [DEAR.id]
    summary = ModelRequest(task="summary", messages=[Message(role="user", content="hi")])
    assert [entry.id for entry in router.route(policy, summary, soft=True).candidates] == [CHEAP.id]


def _candidates(*models: str) -> ModelPolicyManifest:
    manifest = parse_manifest(
        {
            "apiVersion": "aios/v1alpha1",
            "kind": "ModelPolicy",
            "metadata": {"name": "default", "version": "1.0.0"},
            "spec": {
                "default": {"candidates": list(models)},
                "budgets": {"perTenantMonthlyUSD": 1.0, "onSoftLimit": "downgrade"},
            },
        }
    )
    assert isinstance(manifest, ModelPolicyManifest)
    return manifest


def test_with_no_rule_the_cheapest_candidate_goes_first() -> None:
    """The policy in requirements 7.10 says ``downgrade`` with no soft rule, and still means it."""
    router = Router(ModelRegistry([DEAR, CHEAP]))
    route = router.route(_candidates(DEAR.id, CHEAP.id), _request(), soft=True)
    assert [entry.id for entry in route.candidates] == [CHEAP.id, DEAR.id]
    assert route.downgraded is True


def test_below_the_limit_the_order_stays() -> None:
    router = Router(ModelRegistry([DEAR, CHEAP]))
    route = router.route(_candidates(DEAR.id, CHEAP.id), _request())
    assert [entry.id for entry in route.candidates] == [DEAR.id, CHEAP.id]
    assert route.downgraded is False


def test_already_the_cheapest_isnt_a_downgrade() -> None:
    router = Router(ModelRegistry([DEAR, CHEAP]))
    route = router.route(_candidates(CHEAP.id, DEAR.id), _request(), soft=True)
    assert [entry.id for entry in route.candidates] == [CHEAP.id, DEAR.id]
    assert route.downgraded is False


def test_a_model_with_no_price_isnt_taken_for_a_cheap_one() -> None:
    router = Router(ModelRegistry([DEAR, PRIVATE]))
    route = router.route(_candidates(PRIVATE.id, DEAR.id), _request(), soft=True)
    assert [entry.id for entry in route.candidates] == [DEAR.id, PRIVATE.id]


def test_the_cheapest_is_still_only_what_the_guard_allows() -> None:
    router = Router(ModelRegistry([DEAR, CHEAP, PRIVATE]))
    policy = _candidates(CHEAP.id, PRIVATE.id)
    restricted = _request(data_class=DataClass.RESTRICTED)
    route = router.route(policy, restricted, soft=True)
    assert [entry.id for entry in route.candidates] == [PRIVATE.id]


async def test_the_record_says_a_reordered_route_was_a_downgrade(bus: InMemoryEventBus) -> None:
    provider = Fake()
    kernel = _kernel(bus, _candidates(DEAR.id, CHEAP.id), provider)
    site = _site()
    await _spend(kernel, site, "tenant", 0.8)
    await kernel.generate(_request(), site)
    assert provider.calls == [CHEAP.id]
    [done] = await _of_type(bus, "model.call.completed")
    assert ModelCallCompleted.model_validate(done.data).downgraded is True


# --------------------------------------------------------------------------- paying


async def test_a_call_is_charged_to_everyone_paying(bus: InMemoryEventBus) -> None:
    kernel = _kernel(bus, _policy({}), Fake())
    site = _site()
    await kernel.generate(_request(), site)
    paying = subjects_of(site, datetime.now(UTC))
    assert set(paying) == {"run", "user", "app", "tenant"}
    spent = await kernel.ledger.spent(site.event.tenantid, list(paying.values()))
    assert spent == dict.fromkeys(paying.values(), pytest.approx(CALL_COST))


async def test_an_embedding_is_charged_too(bus: InMemoryEventBus) -> None:
    kernel = _kernel(bus, _policy({}), Fake())
    site = _site()
    await kernel.embed(EmbeddingRequest(task="embedding", texts=["hi"]), site)
    tenant = subjects_of(site, datetime.now(UTC))["tenant"]
    spent = await kernel.ledger.spent(site.event.tenantid, [tenant])
    assert spent[tenant] == pytest.approx(0.01)


async def test_a_call_read_back_from_the_log_isnt_paid_for_twice(bus: InMemoryEventBus) -> None:
    kernel = _kernel(bus, _policy({}), Fake())
    site = _site()
    first = await kernel.generate(_request(), site)
    again = await kernel.generate(_request(), site)
    assert (first.replayed, again.replayed) == (False, True)
    tenant = subjects_of(site, datetime.now(UTC))["tenant"]
    spent = await kernel.ledger.spent(site.event.tenantid, [tenant])
    assert spent[tenant] == pytest.approx(CALL_COST)


async def test_a_failed_call_costs_nothing(bus: InMemoryEventBus) -> None:
    provider = Fake(ProviderError("no", retryable=False))
    kernel = _kernel(bus, _policy({}), provider)
    site = _site()
    with pytest.raises(Exception, match="every candidate failed"):
        await kernel.generate(_request(), site)
    tenant = subjects_of(site, datetime.now(UTC))["tenant"]
    assert await kernel.ledger.spent(site.event.tenantid, [tenant]) == {}


async def test_a_call_on_behalf_of_nobody_has_no_user_to_charge(bus: InMemoryEventBus) -> None:
    kernel = _kernel(bus, _policy({"perUserMonthlyUSD": 1.0}), Fake())
    event = task_request("planning", step="plan")
    site = CallSite(event=event, agent="planner-agent", principal="agent:x@acme", index=0)
    await kernel.generate(_request(), site)
    assert "user" not in subjects_of(site, datetime.now(UTC))


# --------------------------------------------------------------------------- whole runs

APP = """
---
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 0.1.0}
spec:
  entrypoints: [{type: chat, workflow: ticket}]
  agents: [worker]
  workflows: [ticket]
  modelPolicy: tight%s
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: worker, version: 0.1.0}
spec:
  runtime: {type: python, entrypoint: "budget_handlers:handler"}
  capabilities: [{name: desk.work}]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: ticket, version: 0.1.0}
spec:
  steps:
    - {id: only, type: task, capability: desk.work}
---
apiVersion: aios/v1alpha1
kind: ModelPolicy
metadata: {name: tight, version: 1.0.0}
spec:
  rules:
    - {match: {budget: soft}, candidates: [fake/cheap]}
  default: {candidates: [fake/dear]}
  budgets: {%s, onSoftLimit: downgrade}
"""
NO_APP_BUDGETS = ""
Handler = Callable[[Context, Any], Awaitable[Any]]


async def _thrifty(ctx: Context, task: Any) -> str:
    for number in range(3):
        await ctx.model.generate(messages=f"question {number}")
    return "done"


OpenDesk = Callable[..., Awaitable[tuple[LocalRuntime, Fake]]]


@pytest.fixture
async def open_desk(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[OpenDesk]:
    running: list[LocalRuntime] = []

    async def make(budgets: str, app: str = NO_APP_BUDGETS) -> tuple[LocalRuntime, Fake]:
        module = ModuleType("budget_handlers")
        module.handler = _thrifty  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "budget_handlers", module)
        provider = Fake()
        runtime = LocalRuntime(tenant="acme", providers={"fake": provider})
        await runtime.__aenter__()
        running.append(runtime)
        runtime.runtime.models.registry.add(DEAR)
        runtime.runtime.models.registry.add(CHEAP)
        await runtime.runtime.apply(load_manifests(APP % (app, budgets)), tenant="acme")
        return runtime, provider

    yield make
    for runtime in running:
        await runtime.__aexit__(None, None, None)


async def test_a_run_over_budget_stops_cleanly(open_desk: OpenDesk) -> None:
    """M2 exit 4: stopped by ``budget.exceeded``, in the middle of a task, and not tried again."""
    runtime, provider = await open_desk("perRunUSD: 0.03")
    result = await (await runtime.chat("desk", "hello", tenant="acme")).wait(timeout=10)
    assert result.status == "failed"
    types = [event.type for event in result.events]
    assert "step.retrying" not in types
    [stop] = [event for event in result.events if event.type == "budget.exceeded"]
    assert BudgetExceeded.model_validate(stop.data).level == "run"
    [failed] = [event for event in result.events if event.type == "run.failed"]
    assert RunFailed.model_validate(failed.data).error.code == "budget.exceeded"
    assert len(provider.calls) == 2  # the third call is the one a hard limit denies


async def test_a_tenant_at_its_soft_limit_gets_the_cheaper_model(open_desk: OpenDesk) -> None:
    """M2 exit 4: one run spends up to the soft limit; the rest of the month runs cheaper."""
    runtime, provider = await open_desk("perTenantMonthlyUSD: 0.05, softAt: 0.8")
    first = await (await runtime.chat("desk", "hello", tenant="acme")).wait(timeout=10)
    assert first.status == "completed"
    assert provider.calls == [DEAR.id, DEAR.id, CHEAP.id]
    second = await (await runtime.chat("desk", "hello", tenant="acme")).wait(timeout=10)
    assert second.status == "completed"
    assert provider.calls[3:] == [CHEAP.id, CHEAP.id, CHEAP.id]
    warnings = [event for event in first.events if event.type == "budget.warning"]
    assert len(warnings) == 1
    assert not [event for event in second.events if event.type == "budget.warning"]


async def test_an_apps_own_limits_count_once_it_is_installed(open_desk: OpenDesk) -> None:
    runtime, provider = await open_desk("softAt: 1.0", "\n  budgets: {monthlyUSD: 0.03}")
    result = await (await runtime.chat("desk", "hello", tenant="acme")).wait(timeout=10)
    assert result.status == "failed"
    [stop] = [event for event in result.events if event.type == "budget.exceeded"]
    assert BudgetExceeded.model_validate(stop.data).level == "app"
    assert len(provider.calls) == 2


# --------------------------------------------------------------------------- metrics


async def test_budgets_show_up_in_the_metrics(bus: InMemoryEventBus) -> None:
    budgets = {"perTenantMonthlyUSD": 1.0, "onSoftLimit": "downgrade"}
    kernel = _kernel(bus, _policy(budgets, SOFT_RULE), Fake())
    site = _site()
    await _spend(kernel, site, "tenant", 0.8)
    await kernel.generate(_request(), site)
    metrics = EventMetrics(bus)
    for stored in await _of_type(bus, ">"):
        if stored.type != "budget.warning" or BudgetWarning.model_validate(stored.data).limit == 1:
            metrics.observe(stored)
    stop = BudgetExceeded(budget="cost", limit=1.0, observed=1.0, level="tenant")
    metrics.observe(
        site.event.derive(
            "budget.exceeded",
            stop.model_dump(mode="json"),
            source="aios://acme/system/test",
            principal="system:test",
        )
    )
    text = metrics.exposition().decode()
    assert 'aios_model_downgrades_total{app="ai-researcher",model="cheap"' in text
    assert 'aios_budget_warnings_total{app="ai-researcher",level="tenant"' in text
    stopped = 'aios_budget_exceeded_total{app="ai-researcher",budget="cost",level="tenant"'
    assert stopped in text

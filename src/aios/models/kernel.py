"""The Model Kernel (WP 1.4; FR-MOD-01, 03, 05, 07, 12; ADR-008, ADR-010).

``generate`` is one governed model call:

1. **Reuse.** If the run's log already holds a ``model.call.completed`` for this call site
   (the task is running again, after a crash or an approval), return the recorded output: a
   completed call is never made again (M1 exit criterion 2).
2. **Route** with the App's model policy, once the Security Kernel allows the agent to call a
   model at all (the kill switch stops it here) and its budgets allow it (FR-MOD-08): a hard limit
   denies the call, a soft one warns and can move its route. Restricted data only goes to private
   deployments.
3. **Call** the candidates in order, retrying retryable errors with backoff and falling back to
   the next candidate otherwise (FR-MOD-05). A cassette replays or records the call.
4. **Account and record.** Tokens and cost come from the registry's prices; the call, with its
   full output, is published as ``model.call.completed`` (or ``model.call.failed``), and paid for
   in the same step.

``embed`` is governed the same way, except for reuse: vectors are too large for the log, so a
task that runs again embeds again, and both calls are recorded because both are billed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import cast

import httpx2
from pydantic import JsonValue

from aios.core.cassettes import Cassette, request_hash
from aios.core.catalog import (
    BudgetLevel,
    BudgetWarning,
    ErrorInfo,
    ModelCallCompleted,
    ModelCallFailed,
    ModelToolCall,
    Usage,
)
from aios.core.envelope import DataClass, Event
from aios.core.errors import AIOSError, BudgetExceededError, ModelError, ReplayDivergedError
from aios.core.manifests import (
    AppBudgets,
    ModelBudgets,
    ModelDefault,
    ModelMatch,
    ModelPolicyManifest,
    ModelRule,
    load_manifests,
)
from aios.core.policy import (
    Actor,
    AllowAll,
    PolicyAction,
    PolicyDecisionPoint,
    PolicyRequest,
    Resource,
)
from aios.core.ports import (
    CallSite,
    EmbeddingRequest,
    EmbeddingResponse,
    ModelGateway,
    ModelRequest,
    ModelResponse,
)
from aios.core.replay import as_source, recorded_error, replaying, task_source
from aios.fabric.port import EventBus, Scope
from aios.models.ledger import InMemorySpendLedger, SpendLedger, subjects_of
from aios.models.providers import (
    AnthropicProvider,
    Completion,
    Embedding,
    OpenAIProvider,
    Provider,
    ProviderError,
)
from aios.models.registry import ModelEntry, ModelRegistry
from aios.models.router import Router

logger = logging.getLogger("aios.models")

DEFAULT_POLICY = "default"
NO_REASONING = {"chat_template_kwargs": {"enable_thinking": False}}
"""Reasoning shares a private deployment's small context with the answer, and can exhaust it."""


def default_policy() -> ModelPolicyManifest:
    text = resources.files("aios.models").joinpath("defaults/model-policy.yaml").read_text("utf-8")
    [policy] = load_manifests(text)
    assert isinstance(policy, ModelPolicyManifest)  # noqa: S101 - the shipped file
    return policy


@dataclass(frozen=True, slots=True)
class _Served[T]:
    entry: ModelEntry
    result: T
    failures: list[str]
    downgraded: bool


class ModelKernel(ModelGateway):
    def __init__(
        self,
        bus: EventBus,
        *,
        registry: ModelRegistry | None = None,
        policies: Mapping[str, ModelPolicyManifest] | None = None,
        providers: Mapping[str, Provider] | None = None,
        cassette: Cassette | None = None,
        decisions: PolicyDecisionPoint | None = None,
        ledger: SpendLedger | None = None,
        retry_delay: float = 0.5,
    ) -> None:
        self.bus = bus
        self.decisions = decisions or AllowAll()
        self.registry = registry or ModelRegistry.default()
        self.router = Router(self.registry)
        self.policies: dict[str, ModelPolicyManifest] = dict(
            policies or {DEFAULT_POLICY: default_policy()}
        )
        self.providers: dict[str, Provider] = dict(providers or {})
        self.cassette = cassette
        self.ledger = ledger or InMemorySpendLedger(bus)
        self.retry_delay = retry_delay
        self._app_policies: dict[tuple[str, str], str] = {}
        self._app_budgets: dict[tuple[str, str], AppBudgets] = {}
        self._warned: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------ configuration

    def set_policy(self, policy: ModelPolicyManifest) -> None:
        self.policies[policy.metadata.name] = policy

    def use_policy(self, tenant: str, app: str, name: str) -> None:
        """The policy an App's calls use (``spec.modelPolicy``)."""
        self._app_policies[(tenant, app)] = name

    def use_budgets(self, tenant: str, app: str, budgets: AppBudgets) -> None:
        """An App's own limits (``spec.budgets``). The tighter of these and its policy's holds."""
        self._app_budgets[(tenant, app)] = budgets

    def use_private_model(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        embedding_model: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        """Route every task of the default policy to one OpenAI-compatible private server.

        ``embedding_model`` serves embeddings when the chat model can't.
        """
        entry = ModelEntry(id=f"private/{model}", model=model, deployment="private")
        self.registry.add(entry)
        self.providers["private"] = OpenAIProvider(
            name="private",
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
            extra_body=NO_REASONING,
            http_client=http_client,
        )
        rules: list[ModelRule] = []
        if embedding_model is not None:
            embedder = ModelEntry(
                id=f"private/{embedding_model}", model=embedding_model, deployment="private"
            )
            self.registry.add(embedder)
            rules = [ModelRule(match=ModelMatch(task="embedding"), candidates=[embedder.id])]
        policy = self.policies[DEFAULT_POLICY]
        spec = policy.spec.model_copy(
            update={"rules": rules, "default": ModelDefault(candidates=[entry.id])}
        )
        self.set_policy(policy.model_copy(update={"spec": spec}))

    def budget_limits(self, tenant: str, app: str) -> tuple[dict[BudgetLevel, float], float]:
        """The hard limits that hold for an App's calls, and the fraction that is soft."""
        budgets = self.policy_for(tenant, app).spec.budgets
        return _limits(budgets, self._app_budgets.get((tenant, app))), budgets.soft_at

    def policy_for(self, tenant: str, app: str) -> ModelPolicyManifest:
        name = self._app_policies.get((tenant, app), DEFAULT_POLICY)
        policy = self.policies.get(name)
        if policy is None:
            raise ModelError(f"model policy {name!r} is not installed")
        return policy

    def _provider(self, name: str) -> Provider:
        provider = self.providers.get(name)
        if provider is None:
            if name == "anthropic":
                provider = AnthropicProvider(max_retries=0)
            elif name == "openai":
                provider = OpenAIProvider(max_retries=0)
            elif name == "google":
                from aios.models.gemini import GeminiProvider  # noqa: PLC0415 - slow to import

                provider = GeminiProvider()
            else:
                raise ModelError(f"no adapter for provider {name!r} (register one)")
            self.providers[name] = provider
        return provider

    # ------------------------------------------------------------------ the call

    async def generate(self, request: ModelRequest, site: CallSite) -> ModelResponse:
        recorded = await self._recorded(site)
        if recorded is not None:
            return recorded
        source = task_source(site.event)
        if source is not None and source.mode == "replay":
            return await self._replayed(request, site, source.run)
        served = await self._governed(
            request,
            site,
            lambda entry: self._complete(entry, request, site),
            only=source.model if source is not None else None,
        )
        return await self._completed(site, request, served)

    async def _replayed(self, request: ModelRequest, site: CallSite, run: str) -> ModelResponse:
        """A replay's call, answered from its source's record at the same site. Nothing is called,
        and nothing is spent: the call is on this run's record at no cost."""
        key = site.key("model")
        scope = Scope(site.event.tenantid, site.event.appid)
        dumped = as_source(request.model_dump(mode="json"), site.event.runid, run)
        asked = request_hash(ModelRequest.model_validate(dumped))
        async for stored in self.bus.replay(scope, "model.call.*", run_id=run):
            data = stored.event.data
            if not isinstance(data, dict) or data.get("site") != key:
                continue
            if stored.event.type == "model.call.failed":
                failed = ModelCallFailed.model_validate(data)
                await self.bus.publish(
                    self._event(site, "model.call.failed", failed.model_dump(mode="json"))
                )
                raise recorded_error(failed.error)
            if stored.event.type != "model.call.completed":
                continue
            call = ModelCallCompleted.model_validate(data)
            if call.request_hash is not None and call.request_hash != asked:
                break  # the agent asked something else here
            free = call.usage.model_copy(update={"cost_usd": 0.0})
            copy = call.model_copy(update={"usage": free, "replayed": True})
            await self.bus.publish(
                self._event(site, "model.call.completed", copy.model_dump(mode="json"))
            )
            return ModelResponse(
                text=call.output_text,
                parsed=call.output,
                provider=call.provider,
                model=call.model,
                usage=Usage.model_validate(free.model_dump()),
                latency_ms=call.latency_ms,
                stop_reason=call.stop_reason,
                replayed=True,
                tool_calls=call.tool_calls,
            )
        error = ReplayDivergedError(f"{key}: the run being replayed asked nothing like this here")
        await self._failed(site, request, error, [])
        raise error

    async def embed(self, request: EmbeddingRequest, site: CallSite) -> EmbeddingResponse:
        if replaying(site.event) is not None:  # memory falls back to keywords, as with no model
            raise ModelError("a replay calls no models, so it makes no embeddings")
        served = await self._governed(
            request, site, lambda entry: self._embedding(entry, request, site)
        )
        entry, embedding = served.entry, served.result
        usage = entry.usage(embedding.input_tokens, 0)
        payload = ModelCallCompleted(
            call=site.index,
            site=site.key("model"),
            agent=site.agent,
            step=site.step,
            task=request.task,
            operation="embed",
            provider=entry.provider,
            model=embedding.model,
            usage=usage,
            latency_ms=round(embedding.latency_ms, 1),
            output_text="",
            output={"vectors": len(embedding.vectors), "dimensions": len(embedding.vectors[0])},
            fallback_from=served.failures,
            downgraded=served.downgraded,
        )
        await self._charge(site, payload)
        return EmbeddingResponse(
            vectors=embedding.vectors,
            provider=entry.provider,
            model=embedding.model,
            usage=usage,
            latency_ms=embedding.latency_ms,
        )

    async def can_embed(self, request: EmbeddingRequest, site: CallSite) -> bool:
        """Whether routing finds a model, without a call or a failure on the record."""
        if self.cassette is not None and self.cassette.replaying:
            return self.cassette.has(site.key("model"))
        policy = self.policy_for(site.event.tenantid, site.event.appid)
        labelled = _labelled(request, site)
        return bool(self.router.route(policy, labelled, agent=site.agent).candidates)

    async def _governed[T](
        self,
        request: ModelRequest | EmbeddingRequest,
        site: CallSite,
        call: Callable[[ModelEntry], Awaitable[T]],
        *,
        only: str | None = None,
    ) -> _Served[T]:
        """Route a call and try its candidates; says who answered and who failed first.

        ``only`` is a fork's model: the one candidate, checked like any other."""
        await self._authorize(request, site)
        policy = self.policy_for(site.event.tenantid, site.event.appid)
        try:
            soft = await self._standing(policy.spec.budgets, site)
        except BudgetExceededError as error:
            await self._failed(site, request, error, [])
            raise
        replaying = self.cassette is not None and self.cassette.replaying
        route = self.router.route(
            policy,
            _labelled(request, site),
            agent=site.agent,
            allow_placeholders=replaying,
            soft=soft,
            only=only,
        )
        if not route.candidates:
            error = ModelError(
                f"model policy {policy.metadata.name!r} ({route.rule}) has no usable candidate"
                f" for task {request.task!r}; skipped: {', '.join(route.skipped) or 'none'}"
            )
            await self._failed(site, request, error, [])
            raise error
        failures: list[str] = []
        reasons: list[str] = []
        retries = policy.spec.retries
        for entry in route.candidates:
            for attempt in range(retries.max + 1):
                try:
                    return _Served(entry, await call(entry), failures, route.downgraded)
                except ProviderError as exc:
                    logger.warning("%s attempt %d failed: %s", entry.id, attempt + 1, exc)
                    if exc.retryable and attempt < retries.max and retries.backoff != "none":
                        await asyncio.sleep(self.retry_delay * 2**attempt)
                        continue
                    failures.append(entry.id)
                    reasons.append(f"{entry.id}: {exc}")
                    break
                except ModelError as exc:  # e.g. no adapter for this provider
                    logger.warning("%s unavailable: %s", entry.id, exc)
                    failures.append(entry.id)
                    reasons.append(f"{entry.id}: {exc}")
                    break
        error = ModelError(f"every candidate failed: {'; '.join(reasons)}")
        await self._failed(site, request, error, failures)
        raise error

    async def _standing(self, budgets: ModelBudgets, site: CallSite) -> bool:
        """Refuse a call past a hard limit, and say whether a soft limit moves its route."""
        tenant, app = site.event.tenantid, site.event.appid
        limits = _limits(budgets, self._app_budgets.get((tenant, app)))
        paying = subjects_of(site, datetime.now(UTC))
        watched: dict[BudgetLevel, str] = {
            level: paying[level] for level in limits if level in paying
        }
        if not watched:
            return False
        spent = await self.ledger.spent(tenant, list(watched.values()))
        observed: dict[BudgetLevel, float] = {  # to the eight places costs are kept in
            level: round(spent.get(subject, 0.0), 8) for level, subject in watched.items()
        }
        for level, total in observed.items():
            if total >= limits[level]:
                raise BudgetExceededError("cost", limits[level], total, level=level)
        soft: dict[BudgetLevel, float] = {
            level: round(limits[level] * budgets.soft_at, 8) for level in observed
        }
        reached: list[BudgetLevel] = [
            level for level, total in observed.items() if total >= soft[level]
        ]
        for level in reached:
            await self._warn(
                site,
                budgets,
                level,
                limit=limits[level],
                total=observed[level],
                subject=watched[level],
            )
        if reached and budgets.on_soft_limit == "deny":
            level = reached[0]
            raise BudgetExceededError("cost", soft[level], observed[level], level=level)
        return bool(reached) and budgets.on_soft_limit == "downgrade"

    async def _warn(
        self,
        site: CallSite,
        budgets: ModelBudgets,
        level: BudgetLevel,
        *,
        limit: float,
        total: float,
        subject: str,
    ) -> None:
        tenant = site.event.tenantid
        if (tenant, subject) in self._warned:
            return
        payload = BudgetWarning(
            level=level,
            limit=limit,
            soft_limit=round(limit * budgets.soft_at, 8),
            observed=total,
            action=budgets.on_soft_limit,
        )
        event = self._event(site, "budget.warning", payload.model_dump(mode="json"))
        await self.ledger.warn_once(tenant, subject, event)
        self._warned.add((tenant, subject))

    async def _authorize(self, request: ModelRequest | EmbeddingRequest, site: CallSite) -> None:
        decision = await self.decisions.decide(
            PolicyRequest(
                tenant=site.event.tenantid,
                app=site.event.appid,
                actor=Actor(site.principal),
                action=PolicyAction.MODEL_ROUTE,
                resource=Resource.tenant(site.event.tenantid),
            )
        )
        if not decision.allowed:
            error = ModelError(f"model call refused: {decision.reason or 'denied by policy'}")
            await self._failed(site, request, error, [])
            raise error

    async def _complete(
        self, entry: ModelEntry, request: ModelRequest, site: CallSite
    ) -> Completion:
        cassette = self.cassette
        key = site.key("model")
        if cassette is not None and cassette.replaying:
            hashed = request_hash(request)
            drifted = cassette.drifted(key, hashed)
            recorded = cassette.play(key, "model", hashed)
            return Completion(
                text=str(recorded["text"]),
                input_tokens=int(recorded.get("input_tokens", 0)),
                output_tokens=int(recorded.get("output_tokens", 0)),
                model=entry.model,  # the routed model answers in replay
                stop_reason=recorded.get("stop_reason"),
                latency_ms=float(recorded.get("latency_ms", 0.0)),
                cache_read_tokens=int(recorded.get("cache_read_tokens", 0)),
                cache_write_tokens=int(recorded.get("cache_write_tokens", 0)),
                drifted=drifted,
                tool_calls=tuple(
                    ModelToolCall.model_validate(call)
                    for call in cast(list[JsonValue], recorded.get("tool_calls") or [])
                ),
            )
        if entry.placeholder:
            raise ModelError(f"{entry.id} is a placeholder; fill it in the model registry")
        completion = await self._provider(entry.provider).complete(entry, _labelled(request, site))
        if cassette is not None and cassette.recording:
            cassette.record(
                key,
                "model",
                request_hash(request),
                {
                    "text": completion.text,
                    "input_tokens": completion.input_tokens,
                    "output_tokens": completion.output_tokens,
                    "cache_read_tokens": completion.cache_read_tokens,
                    "cache_write_tokens": completion.cache_write_tokens,
                    "stop_reason": completion.stop_reason,
                    "latency_ms": round(completion.latency_ms, 1),
                    "model": completion.model,
                    **_recorded_calls(completion),
                },
            )
        return completion

    async def _embedding(
        self, entry: ModelEntry, request: EmbeddingRequest, site: CallSite
    ) -> Embedding:
        cassette = self.cassette
        key = site.key("model")
        if cassette is not None and cassette.replaying:
            recorded = cassette.play(key, "model", request_hash(request))
            return Embedding(
                vectors=recorded["vectors"],
                input_tokens=int(recorded.get("input_tokens", 0)),
                model=entry.model,
            )
        if entry.placeholder:
            raise ModelError(f"{entry.id} is a placeholder; fill it in the model registry")
        embedding = await self._provider(entry.provider).embed(entry, request)
        if cassette is not None and cassette.recording:
            cassette.record(
                key,
                "model",
                request_hash(request),
                {
                    "vectors": cast(JsonValue, embedding.vectors),
                    "input_tokens": embedding.input_tokens,
                    "model": embedding.model,
                },
            )
        return embedding

    async def _completed(
        self, site: CallSite, request: ModelRequest, served: _Served[Completion]
    ) -> ModelResponse:
        entry, completion = served.entry, served.result
        usage = entry.usage(
            completion.input_tokens,
            completion.output_tokens,
            completion.cache_read_tokens,
            completion.cache_write_tokens,
        )
        parsed = _parsed(completion.text) if request.output_schema is not None else None
        payload = ModelCallCompleted(
            request_hash=request_hash(request),
            call=site.index,
            site=site.key("model"),
            agent=site.agent,
            step=site.step,
            task=request.task,
            provider=entry.provider,
            model=completion.model,
            usage=usage,
            latency_ms=round(completion.latency_ms, 1),
            stop_reason=completion.stop_reason,
            output_text=completion.text,
            output=parsed,
            fallback_from=served.failures,
            downgraded=served.downgraded,
            drifted=completion.drifted,
            tool_calls=list(completion.tool_calls),
        )
        await self._charge(site, payload)
        return ModelResponse(
            text=completion.text,
            parsed=parsed,
            provider=entry.provider,
            model=completion.model,
            usage=usage,
            latency_ms=completion.latency_ms,
            stop_reason=completion.stop_reason,
            tool_calls=list(completion.tool_calls),
        )

    async def _failed(
        self,
        site: CallSite,
        request: ModelRequest | EmbeddingRequest,
        error: AIOSError,
        candidates: list[str],
    ) -> None:
        payload = ModelCallFailed(
            call=site.index,
            site=site.key("model"),
            agent=site.agent,
            step=site.step,
            task=request.task,
            error=ErrorInfo(
                code=error.code,
                message=str(error),
                retryable=not isinstance(error, BudgetExceededError),
            ),
            candidates=candidates,
        )
        await self.bus.publish(
            self._event(site, "model.call.failed", payload.model_dump(mode="json"))
        )

    async def _charge(self, site: CallSite, payload: ModelCallCompleted) -> None:
        event = self._event(site, "model.call.completed", payload.model_dump(mode="json"))
        paying = subjects_of(site, datetime.now(UTC))
        await self.ledger.charge(
            site.event.tenantid, list(paying.values()), payload.usage.cost_usd, event
        )

    def _event(self, site: CallSite, type: str, data: dict[str, object]) -> Event:
        return site.event.derive(
            type,
            data,  # type: ignore[arg-type]
            source=f"aios://{site.event.tenantid}/system/models",
            principal=site.principal,
            dataclass=site.data_class,
            tainted=site.tainted,
        )

    async def _recorded(self, site: CallSite) -> ModelResponse | None:
        """A completed call recorded for this site by an earlier execution of the same task."""
        event = site.event
        if event.runid is None:
            return None
        key = site.key("model")
        scope = Scope(event.tenantid, event.appid)
        async for stored in self.bus.replay(scope, "model.call.completed", run_id=event.runid):
            data = stored.event.data
            if (
                site.recorded_by(stored.event)
                and isinstance(data, dict)
                and data.get("site") == key
            ):
                call = ModelCallCompleted.model_validate(data)
                return ModelResponse(
                    text=call.output_text,
                    parsed=call.output,
                    provider=call.provider,
                    model=call.model,
                    usage=Usage.model_validate(call.usage.model_dump()),
                    latency_ms=call.latency_ms,
                    stop_reason=call.stop_reason,
                    replayed=True,
                    tool_calls=call.tool_calls,
                )
        return None


def _recorded_calls(completion: Completion) -> dict[str, JsonValue]:
    """A completion's tool calls, for a cassette. With none, nothing is added, so older
    recordings stay as they are."""
    if not completion.tool_calls:
        return {}
    return {"tool_calls": [call.model_dump(mode="json") for call in completion.tool_calls]}


def _limits(budgets: ModelBudgets, app: AppBudgets | None) -> dict[BudgetLevel, float]:
    """The limits that hold for one call, where an App and its policy can both set one."""

    def tightest(*values: float | None) -> float | None:
        return min((value for value in values if value is not None), default=None)

    limits: dict[BudgetLevel, float | None] = {
        "run": tightest(budgets.per_run_usd, app.per_run_usd if app else None),
        "user": budgets.per_user_monthly_usd,
        "app": tightest(budgets.per_app_monthly_usd, app.monthly_usd if app else None),
        "tenant": budgets.per_tenant_monthly_usd,
    }
    return {level: usd for level, usd in limits.items() if usd is not None}


def _labelled[R: ModelRequest | EmbeddingRequest](request: R, site: CallSite) -> R:
    """Whoever builds a request can't lower its label below what the caller has read (FR-MOD-06).

    The text of a request can come from anything the agent has seen, so routing takes the strictest
    of what the request says, what the agent read and what the run carries.
    """
    label = DataClass.strictest(request.data_class, site.data_class, site.event.dataclass)
    return request.model_copy(update={"data_class": label})


def _parsed(text: str) -> JsonValue:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0] if "\n" in stripped else ""
    try:
        return cast(JsonValue, json.loads(stripped))
    except ValueError:
        return None

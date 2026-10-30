"""The Model Kernel (WP 1.4; FR-MOD-01, 03, 05, 07, 12; ADR-008, ADR-010).

``generate`` is one governed model call:

1. **Reuse.** If the run's log already holds a ``model.call.completed`` for this call site
   (the task is being re-executed after a crash), return the recorded output: a completed call
   is never made again (M1 exit criterion 2).
2. **Route** with the App's model policy; restricted data only goes to private deployments.
3. **Call** the candidates in order, retrying retryable errors with backoff and falling back to
   the next candidate otherwise (FR-MOD-05). A cassette replays or records the call.
4. **Account and record.** Tokens and cost come from the registry's prices; the call, with its
   full output, is published as ``model.call.completed`` (or ``model.call.failed``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from importlib import resources
from typing import cast

from pydantic import JsonValue

from aios.core.cassettes import Cassette, request_hash
from aios.core.catalog import ErrorInfo, ModelCallCompleted, ModelCallFailed, Usage
from aios.core.errors import ModelError
from aios.core.manifests import ModelPolicyManifest, load_manifests
from aios.core.ports import CallSite, ModelGateway, ModelRequest, ModelResponse
from aios.fabric.port import EventBus, Scope
from aios.models.providers import (
    AnthropicProvider,
    Completion,
    OpenAIProvider,
    Provider,
    ProviderError,
)
from aios.models.registry import ModelEntry, ModelRegistry
from aios.models.router import Router

logger = logging.getLogger("aios.models")

DEFAULT_POLICY = "default"


def default_policy() -> ModelPolicyManifest:
    text = resources.files("aios.models").joinpath("defaults/model-policy.yaml").read_text("utf-8")
    [policy] = load_manifests(text)
    assert isinstance(policy, ModelPolicyManifest)  # noqa: S101 - the shipped file
    return policy


class ModelKernel(ModelGateway):
    def __init__(
        self,
        bus: EventBus,
        *,
        registry: ModelRegistry | None = None,
        policies: Mapping[str, ModelPolicyManifest] | None = None,
        providers: Mapping[str, Provider] | None = None,
        cassette: Cassette | None = None,
        retry_delay: float = 0.5,
    ) -> None:
        self.bus = bus
        self.registry = registry or ModelRegistry.default()
        self.router = Router(self.registry)
        self.policies: dict[str, ModelPolicyManifest] = dict(
            policies or {DEFAULT_POLICY: default_policy()}
        )
        self.providers: dict[str, Provider] = dict(providers or {})
        self.cassette = cassette
        self.retry_delay = retry_delay
        self._app_policies: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------ configuration

    def set_policy(self, policy: ModelPolicyManifest) -> None:
        self.policies[policy.metadata.name] = policy

    def use_policy(self, tenant: str, app: str, name: str) -> None:
        """The policy an App's calls use (``spec.modelPolicy``)."""
        self._app_policies[(tenant, app)] = name

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
            else:
                raise ModelError(f"no adapter for provider {name!r} (register one)")
            self.providers[name] = provider
        return provider

    # ------------------------------------------------------------------ the call

    async def generate(self, request: ModelRequest, site: CallSite) -> ModelResponse:
        recorded = await self._recorded(site)
        if recorded is not None:
            return recorded
        policy = self.policy_for(site.event.tenantid, site.event.appid)
        replaying = self.cassette is not None and self.cassette.replaying
        route = self.router.route(policy, request, agent=site.agent, allow_placeholders=replaying)
        if not route.candidates:
            error = ModelError(
                f"model policy {policy.metadata.name!r} ({route.rule}) has no usable candidate"
                f" for task {request.task!r}; skipped: {', '.join(route.skipped) or 'none'}"
            )
            await self._failed(site, request, error, [])
            raise error
        failures: list[str] = []
        retries = policy.spec.retries
        for entry in route.candidates:
            for attempt in range(retries.max + 1):
                try:
                    completion = await self._complete(entry, request, site)
                except ProviderError as exc:
                    logger.warning("%s attempt %d failed: %s", entry.id, attempt + 1, exc)
                    if exc.retryable and attempt < retries.max and retries.backoff != "none":
                        await asyncio.sleep(self.retry_delay * 2**attempt)
                        continue
                    failures.append(entry.id)
                    break
                except ModelError as exc:  # e.g. no adapter for this provider
                    logger.warning("%s unavailable: %s", entry.id, exc)
                    failures.append(entry.id)
                    break
                return await self._completed(site, request, entry, completion, failures)
        error = ModelError(f"every candidate failed: {', '.join(failures)}")
        await self._failed(site, request, error, failures)
        raise error

    async def _complete(
        self, entry: ModelEntry, request: ModelRequest, site: CallSite
    ) -> Completion:
        cassette = self.cassette
        key = site.key("model")
        if cassette is not None and cassette.replaying:
            recorded = cassette.play(key, "model", request_hash(request))
            return Completion(
                text=str(recorded["text"]),
                input_tokens=int(recorded.get("input_tokens", 0)),
                output_tokens=int(recorded.get("output_tokens", 0)),
                model=entry.model,  # the routed model answers in replay
                stop_reason=recorded.get("stop_reason"),
                latency_ms=float(recorded.get("latency_ms", 0.0)),
            )
        if entry.placeholder:
            raise ModelError(f"{entry.id} is a placeholder; fill it in the model registry")
        completion = await self._provider(entry.provider).complete(entry, request)
        if cassette is not None and cassette.recording:
            cassette.record(
                key,
                "model",
                request_hash(request),
                {
                    "text": completion.text,
                    "input_tokens": completion.input_tokens,
                    "output_tokens": completion.output_tokens,
                    "stop_reason": completion.stop_reason,
                    "latency_ms": round(completion.latency_ms, 1),
                    "model": completion.model,
                },
            )
        return completion

    async def _completed(
        self,
        site: CallSite,
        request: ModelRequest,
        entry: ModelEntry,
        completion: Completion,
        failures: list[str],
    ) -> ModelResponse:
        usage = entry.usage(completion.input_tokens, completion.output_tokens)
        parsed = _parsed(completion.text) if request.output_schema is not None else None
        payload = ModelCallCompleted(
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
            fallback_from=failures,
        )
        await self._publish(site, "model.call.completed", payload.model_dump(mode="json"))
        return ModelResponse(
            text=completion.text,
            parsed=parsed,
            provider=entry.provider,
            model=completion.model,
            usage=usage,
            latency_ms=completion.latency_ms,
            stop_reason=completion.stop_reason,
        )

    async def _failed(
        self, site: CallSite, request: ModelRequest, error: ModelError, candidates: list[str]
    ) -> None:
        payload = ModelCallFailed(
            call=site.index,
            site=site.key("model"),
            agent=site.agent,
            step=site.step,
            task=request.task,
            error=ErrorInfo(code=error.code, message=str(error), retryable=True),
            candidates=candidates,
        )
        await self._publish(site, "model.call.failed", payload.model_dump(mode="json"))

    async def _publish(self, site: CallSite, type: str, data: dict[str, object]) -> None:
        event = site.event.derive(
            type,
            data,  # type: ignore[arg-type]
            source=f"aios://{site.event.tenantid}/system/models",
            principal=site.principal,
        )
        await self.bus.publish(event)

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
                stored.event.causationid == event.id
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
                )
        return None


def _parsed(text: str) -> JsonValue:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0] if "\n" in stripped else ""
    try:
        return cast(JsonValue, json.loads(stripped))
    except ValueError:
        return None

"""Tool OS: the registry and the invocation pipeline (WP 1.5; FR-TOOL-01, 03, 05, 07, 08, 09).

Every call runs the FR-TOOL-05 pipeline and leaves an audit trail on the fabric:

``tool.call.requested`` → resolve the binding → validate the input → policy decision (the model
can't grant itself permissions, FR-SEC-04) → execute with a timeout (or replay a cassette) →
validate and truncate the output → mark it untrusted (FR-SEC-09) → ``tool.call.completed``, or
``tool.call.failed`` at the first failing stage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jsonschema
from pydantic import JsonValue

from aios.core.cassettes import Cassette, request_hash
from aios.core.catalog import ErrorInfo, ToolCallCompleted, ToolCallFailed, ToolCallRequested
from aios.core.errors import (
    AIOSError,
    ApprovalRequiredError,
    ToolDeniedError,
    ToolError,
    ToolInputError,
    ToolNotFoundError,
)
from aios.core.policy import (
    Actor,
    Effect,
    PolicyAction,
    PolicyDecisionPoint,
    PolicyRequest,
    Resource,
)
from aios.core.ports import CallSite, ToolGateway, ToolResult
from aios.fabric.port import EventBus, Scope
from aios.tools.providers import ToolProvider, ToolSpec

logger = logging.getLogger("aios.tools")

MAX_OUTPUT_CHARS = 50_000


@dataclass(frozen=True, slots=True)
class Binding:
    spec: ToolSpec
    provider: ToolProvider


class ToolRegistry:
    """Qualified tool names bound to providers, with description pinning (FR-TOOL-09)."""

    def __init__(self) -> None:
        self._bindings: dict[str, Binding] = {}
        self._pins: dict[str, str] = {}
        self._blocked: dict[str, str] = {}
        self._providers: dict[str, ToolProvider] = {}

    async def bind(self, provider: ToolProvider) -> list[ToolSpec]:
        """Register a provider and approve its tools as listed now (an admin action).

        Replaces an earlier provider of the same name and its tools.
        """
        previous = self._providers.get(provider.name)
        if previous is not None and previous is not provider:
            self._bindings = {
                name: b for name, b in self._bindings.items() if b.provider is not previous
            }
            await previous.close()
        self._providers[provider.name] = provider
        specs = await provider.list_tools()
        for spec in specs:
            self._pins[spec.name] = spec.description_hash
            self._blocked.pop(spec.name, None)
            self._bindings[spec.name] = Binding(spec, provider)
        return specs

    async def refresh(self, name: str) -> list[ToolSpec]:
        """Re-list a registered provider's tools (e.g. on MCP ``listChanged``).

        A tool whose description or schema differs from the approved one is blocked until an
        admin approves it again: a server can't change what the model is told on its own.
        """
        provider = self._providers[name]
        specs = await provider.list_tools()
        for spec in specs:
            pinned = self._pins.setdefault(spec.name, spec.description_hash)
            if pinned != spec.description_hash:
                self._blocked[spec.name] = (
                    "the tool's description or schema changed since it was approved"
                )
                logger.warning("tool %s changed; blocked until approved again", spec.name)
            self._bindings[spec.name] = Binding(spec, provider)
        return specs

    def approve(self, name: str) -> None:
        """Accept a changed tool description (an admin action)."""
        binding = self._bindings[name]
        self._pins[name] = binding.spec.description_hash
        self._blocked.pop(name, None)

    def resolve(self, name: str) -> Binding:
        binding = self._bindings.get(name)
        if binding is None:
            raise ToolNotFoundError(f"no tool {name!r} is registered")
        if name in self._blocked:
            raise ToolDeniedError(f"{name}: {self._blocked[name]}")
        return binding

    def specs(self) -> list[ToolSpec]:
        return [binding.spec for binding in self._bindings.values()]

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()


class ToolService(ToolGateway):
    def __init__(
        self,
        bus: EventBus,
        registry: ToolRegistry,
        policy: PolicyDecisionPoint,
        *,
        cassette: Cassette | None = None,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ) -> None:
        self.bus = bus
        self.registry = registry
        self.policy = policy
        self.cassette = cassette
        self.max_output_chars = max_output_chars

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, JsonValue],
        site: CallSite,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        args = dict(arguments)
        recorded = await self._recorded(site, name)
        if recorded is not None:
            return recorded
        await self._publish(
            site,
            "tool.call.requested",
            ToolCallRequested(
                call=site.index,
                site=site.key("tool"),
                tool=name,
                agent=site.agent,
                step=site.step,
                arguments=args,
                idempotency_key=idempotency_key,
            ).model_dump(mode="json"),
        )
        try:
            binding = self.registry.resolve(name)
            self._validate_input(binding.spec, args)
            await self._authorize(binding.spec, args, site)
            started = time.perf_counter()
            output = await self._execute(binding, args, site, idempotency_key)
            latency = (time.perf_counter() - started) * 1000
            output = self._check_output(binding.spec, output)
        except Exception as exc:
            error = (
                exc if isinstance(exc, AIOSError) else ToolError(f"{name}: {exc}", retryable=True)
            )
            await self._failed(site, name, error)
            if error is exc:
                raise
            raise error from exc
        untrusted = not binding.spec.trusted
        await self._publish(
            site,
            "tool.call.completed",
            ToolCallCompleted(
                call=site.index,
                site=site.key("tool"),
                tool=name,
                provider=binding.provider.name,
                latency_ms=round(latency, 1),
                output=output,
                untrusted=untrusted,
            ).model_dump(mode="json"),
        )
        return ToolResult(
            tool=name,
            output=output,
            provider=binding.provider.name,
            untrusted=untrusted,
            latency_ms=latency,
        )

    async def _recorded(self, site: CallSite, name: str) -> ToolResult | None:
        """A call completed at this site by an earlier execution of the same task (NFR-06)."""
        event = site.event
        if event.runid is None:
            return None
        key = site.key("tool")
        scope = Scope(event.tenantid, event.appid)
        async for stored in self.bus.replay(scope, "tool.call.completed", run_id=event.runid):
            data = stored.event.data
            if (
                stored.event.causationid == event.id
                and isinstance(data, dict)
                and data.get("site") == key
            ):
                call = ToolCallCompleted.model_validate(data)
                if call.tool == name:
                    return ToolResult(
                        tool=name,
                        output=call.output,
                        provider=call.provider,
                        untrusted=call.untrusted,
                        latency_ms=call.latency_ms,
                    )
        return None

    async def _failed(self, site: CallSite, name: str, error: AIOSError) -> None:
        await self._publish(
            site,
            "tool.call.failed",
            ToolCallFailed(
                call=site.index,
                site=site.key("tool"),
                tool=name,
                error=ErrorInfo(
                    code=error.code,
                    message=str(error),
                    retryable=bool(getattr(error, "retryable", False)),
                ),
            ).model_dump(mode="json"),
        )

    # ------------------------------------------------------------------ pipeline stages

    @staticmethod
    def _validate_input(spec: ToolSpec, arguments: dict[str, Any]) -> None:
        try:
            jsonschema.validate(arguments, spec.input_schema)
        except jsonschema.ValidationError as exc:
            raise ToolInputError(f"{spec.name}: {exc.message}") from exc

    async def _authorize(self, spec: ToolSpec, arguments: dict[str, Any], site: CallSite) -> None:
        decision = await self.policy.decide(
            PolicyRequest(
                tenant=site.event.tenantid,
                app=site.event.appid,
                actor=Actor(site.principal),
                action=PolicyAction.TOOL_INVOKE,
                resource=Resource.tool(spec.name, spec.side_effect),
                tainted=site.tainted,
                input=arguments,
            )
        )
        if decision.effect is Effect.DENY:
            raise ToolDeniedError(f"{spec.name}: {decision.reason or 'denied by policy'}")
        if decision.effect is Effect.REQUIRE_APPROVAL:
            raise ApprovalRequiredError(
                f"{spec.name}: {decision.reason or 'approval required'}", decision.approvers
            )

    async def _execute(
        self,
        binding: Binding,
        arguments: dict[str, Any],
        site: CallSite,
        idempotency_key: str | None,
    ) -> JsonValue:
        cassette = self.cassette
        key = site.key("tool")
        hashed = request_hash({"tool": binding.spec.name, "arguments": arguments})
        if cassette is not None and cassette.replaying and binding.provider.replayable:
            return cassette.play(key, "tool", hashed)["output"]
        try:
            async with asyncio.timeout(binding.spec.timeout_s):
                output = await binding.provider.call(
                    binding.spec.name, arguments, idempotency_key=idempotency_key
                )
        except TimeoutError as exc:
            raise ToolError(f"{binding.spec.name} timed out", retryable=True) from exc
        if cassette is not None and cassette.recording and binding.provider.replayable:
            cassette.record(key, "tool", hashed, {"output": output})
        return output

    def _check_output(self, spec: ToolSpec, output: JsonValue) -> JsonValue:
        if spec.output_schema is not None:
            try:
                jsonschema.validate(output, spec.output_schema)
            except jsonschema.ValidationError as exc:
                raise ToolError(f"{spec.name} returned invalid output: {exc.message}") from exc
        if len(json.dumps(output, default=str)) > self.max_output_chars:
            text = json.dumps(output, default=str)[: self.max_output_chars]
            return {"truncated": True, "text": text}
        return output

    async def _publish(self, site: CallSite, type: str, data: dict[str, Any]) -> None:
        event = site.event.derive(
            type,
            data,
            source=f"aios://{site.event.tenantid}/system/tools",
            principal=site.principal,
        )
        await self.bus.publish(event)

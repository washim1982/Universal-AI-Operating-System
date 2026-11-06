"""Tool OS: the registry and the invocation pipeline (WP 1.5; FR-TOOL-01, 03, 05 to 09).

Every call runs the FR-TOOL-05 pipeline and leaves an audit trail on the fabric:

``tool.call.requested`` → resolve the binding → validate the input → policy decision (the model
can't grant itself permissions, FR-SEC-04) → ask a person, if a rule says to → inject credentials →
execute with a timeout (or replay a cassette) → validate and truncate the output → mark it
untrusted (FR-SEC-09) → ``tool.call.completed``, or ``tool.call.failed`` at the first failing stage.

Egress is checked with the policy, and an approval can't lift it: nobody should be asked to wave
a request through to a host that isn't on the list (FR-SEC-10).

A call waiting for a person is neither: it stops the task (:class:`AwaitingApproval`), which runs
again once someone answers. Policy is then asked again, with the approval, because an approval only
lifts ``requireApproval``. A kill switch or a ``deny`` that arrived in the meantime still applies.

Secrets never enter prompts, events or logs (FR-TOOL-06): they are fetched after the policy
decision, are not arguments, and are masked in whatever the tool returns or raises. Masking finds
exact copies only, so a tool that encodes a secret can still leak it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import jsonschema
from pydantic import JsonValue

from aios.core.cassettes import Cassette, request_hash
from aios.core.catalog import (
    ErrorInfo,
    PolicyDecided,
    Risk,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
)
from aios.core.envelope import Event
from aios.core.errors import (
    AIOSError,
    EgressDeniedError,
    ReplayDivergedError,
    ToolCredentialError,
    ToolDeniedError,
    ToolError,
    ToolInputError,
    ToolNotFoundError,
)
from aios.core.manifests import Egress as EgressSpec
from aios.core.policy import (
    Actor,
    Effect,
    PolicyAction,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyRequest,
    Resource,
    SideEffect,
)
from aios.core.ports import (
    ApprovalGateway,
    CallSite,
    NoApprovals,
    NoSecrets,
    Proposal,
    SecretStore,
    ToolGateway,
    ToolResult,
)
from aios.core.replay import as_recorded, as_source, holding_back, recorded_error, replaying
from aios.fabric.port import EventBus, Scope
from aios.tools.egress import Egress, host_of, urls_in
from aios.tools.providers import ToolProvider, ToolSpec

logger = logging.getLogger("aios.tools")

MAX_OUTPUT_CHARS = 50_000
REDACTED = "[REDACTED]"
SUMMARY_CHARS = 200
_LOW_RISK = (SideEffect.READ,)
_MEDIUM_RISK = (SideEffect.WRITE,)


def _masked(value: Any, secrets: Sequence[str]) -> Any:
    """``value`` with every copy of a secret replaced, at any depth."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, list):
        return [_masked(item, secrets) for item in cast(list[Any], value)]
    if isinstance(value, dict):
        items = cast(dict[str, Any], value).items()
        return {_masked(key, secrets): _masked(item, secrets) for key, item in items}
    return value


def _summary(name: str, arguments: Mapping[str, Any]) -> str:
    """The call as a person would read it. The payload next to it is the exact one."""
    listed = ", ".join(
        f"{key}={json.dumps(value, default=str)}" for key, value in arguments.items()
    )
    line = f"{name}({listed})"
    return line if len(line) <= SUMMARY_CHARS else f"{line[: SUMMARY_CHARS - 1]}…"


def _risk(spec: ToolSpec, tainted: bool) -> Risk:
    """Outside content in the agent's context makes anything it asks for worth a harder look."""
    if tainted or spec.side_effect not in (*_LOW_RISK, *_MEDIUM_RISK):
        return "high"
    return "low" if spec.side_effect in _LOW_RISK else "medium"


@dataclass(frozen=True, slots=True)
class Binding:
    spec: ToolSpec
    provider: ToolProvider


class ToolRegistry:
    """Qualified tool names bound to providers, with description pinning (FR-TOOL-09).

    Two providers can offer the same tool. The newest serves it, unless :meth:`choose` says
    otherwise for this environment or for one tenant: local disk in development, say, and object
    storage in production (FR-TOOL-03).
    """

    def __init__(self) -> None:
        self._bindings: dict[str, Binding] = {}
        self._offers: dict[tuple[str, str], Binding] = {}
        self._choices: dict[tuple[str | None, str], str] = {}
        self._pins: dict[tuple[str, str], str] = {}
        self._blocked: dict[tuple[str, str], str] = {}
        self._providers: dict[str, ToolProvider] = {}

    async def bind(self, provider: ToolProvider) -> list[ToolSpec]:
        """Register a provider and approve its tools as listed now (an admin action).

        Replaces an earlier provider of the same name and its tools.
        """
        previous = self._providers.get(provider.name)
        if previous is not None and previous is not provider:
            self._offers = {key: b for key, b in self._offers.items() if b.provider is not previous}
            self._bindings = {tool: b for (_, tool), b in self._offers.items()}
            await previous.close()
        self._providers[provider.name] = provider
        specs = await provider.list_tools()
        for spec in specs:
            self._pins[(provider.name, spec.name)] = spec.description_hash
            self._blocked.pop((provider.name, spec.name), None)
            self._offer(spec, provider)
        return specs

    def _offer(self, spec: ToolSpec, provider: ToolProvider) -> None:
        self._bindings[spec.name] = self._offers[(provider.name, spec.name)] = Binding(
            spec, provider
        )

    def choose(self, tool: str, provider: str, *, tenant: str | None = None) -> None:
        """Serve ``tool`` from ``provider``, here or for one tenant (an admin action).

        A choice holds even if the provider isn't there: the call fails, and doesn't land on a
        provider someone decided against.
        """
        self._choices[(tenant, tool)] = provider

    async def refresh(self, name: str) -> list[ToolSpec]:
        """Re-list a registered provider's tools (e.g. on MCP ``listChanged``).

        A tool whose description or schema differs from the approved one is blocked until an
        admin approves it again: a server can't change what the model is told on its own.
        """
        provider = self._providers[name]
        specs = await provider.list_tools()
        for spec in specs:
            pinned = self._pins.setdefault((name, spec.name), spec.description_hash)
            if pinned != spec.description_hash:
                self._blocked[(name, spec.name)] = (
                    "the tool's description or schema changed since it was approved"
                )
                logger.warning("tool %s changed; blocked until approved again", spec.name)
            self._offer(spec, provider)
        return specs

    def approve(self, name: str) -> None:
        """Accept a changed tool description (an admin action)."""
        for (provider, tool), binding in self._offers.items():
            if tool == name:
                self._pins[(provider, tool)] = binding.spec.description_hash
                self._blocked.pop((provider, tool), None)

    def resolve(self, name: str, tenant: str | None = None) -> Binding:
        chosen = self._choices.get((tenant, name)) or self._choices.get((None, name))
        binding = self._bindings.get(name) if chosen is None else self._offers.get((chosen, name))
        if binding is None:
            raise ToolNotFoundError(f"no tool {name!r} is registered")
        reason = self._blocked.get((binding.provider.name, name))
        if reason is not None:
            raise ToolDeniedError(f"{name}: {reason}")
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
        secrets: SecretStore | None = None,
        approvals: ApprovalGateway | None = None,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ) -> None:
        self.bus = bus
        self.registry = registry
        self.policy = policy
        self.cassette = cassette
        self.secrets = secrets or NoSecrets()
        self.approvals = approvals or NoApprovals()
        self._egress: dict[tuple[str, str], EgressSpec] = {}
        self.max_output_chars = max_output_chars

    def set_egress(self, tenant: str, app: str, spec: EgressSpec) -> None:
        self._egress[(tenant, app)] = spec

    def egress(self, tenant: str, app: str, tool: str) -> Egress:
        spec = self._egress.get((tenant, app), EgressSpec())
        allow = spec.allow
        named = spec.tools.get(tool)
        return Egress(
            app=tuple(allow) if allow is not None else None,
            tool=tuple(named) if named is not None else None,
        )

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, JsonValue],
        site: CallSite,
        *,
        idempotency_key: str | None = None,
        rationale: str | None = None,
    ) -> ToolResult:
        args = dict(arguments)
        recorded = await self._recorded(site, name)
        if recorded is not None:
            return recorded
        if (source := replaying(site.event)) is not None:
            return await self._replayed(site, name, args, source.run)
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
            binding = self.registry.resolve(name, site.event.tenantid)
            self._validate_input(binding.spec, args)
            held = holding_back(site.event)
            if held is not None and binding.spec.side_effect is not SideEffect.READ:
                return await self._stubbed(binding, args, site, held.run)
            await self._authorize(binding.spec, args, site, rationale)
            started = time.perf_counter()
            output = await self._execute(binding, args, site, idempotency_key)
            latency = (time.perf_counter() - started) * 1000
            output = as_recorded(self._check_output(binding.spec, output))
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
            tainted=untrusted,
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
                site.recorded_by(stored.event)
                and isinstance(data, dict)
                and data.get("site") == key
            ):
                call = ToolCallCompleted.model_validate(data)
                if call.tool == name:
                    return ToolResult(
                        tool=name,
                        output=as_recorded(call.output),
                        provider=call.provider,
                        untrusted=call.untrusted,
                        latency_ms=call.latency_ms,
                    )
        return None

    async def _source_call(self, site: CallSite, name: str, run: str) -> tuple[Any, Event | None]:
        """What a source run asked of ``name`` at this site, and how the call ended there."""
        key = site.key("tool")
        asked: Any = None
        ended: Event | None = None
        scope = Scope(site.event.tenantid, site.event.appid)
        async for stored in self.bus.replay(scope, "tool.call.*", run_id=run):
            data = stored.event.data
            if not isinstance(data, dict) or data.get("site") != key or data.get("tool") != name:
                continue
            if stored.event.type == "tool.call.requested":
                asked = data.get("arguments")
            elif stored.event.type in ("tool.call.completed", "tool.call.failed"):
                ended = stored.event
        return asked, ended

    async def _replayed(
        self, site: CallSite, name: str, args: dict[str, Any], run: str
    ) -> ToolResult:
        """A replay's call, answered from its source's record: the tool isn't called."""
        asked, ended = await self._source_call(site, name, run)
        await self._publish(
            site,
            "tool.call.requested",
            ToolCallRequested(
                call=site.index, site=site.key("tool"), tool=name, agent=site.agent,
                step=site.step, arguments=args,
            ).model_dump(mode="json"),
        )  # fmt: skip
        if ended is None or asked != as_source(args, site.event.runid, run):
            error = ReplayDivergedError(f"{name}: the run being replayed made no such call here")
            await self._failed(site, name, error)
            raise error
        if ended.type == "tool.call.failed":
            failed = ToolCallFailed.model_validate(ended.data)
            await self._publish(site, "tool.call.failed", failed.model_dump(mode="json"))
            raise recorded_error(failed.error)
        call = ToolCallCompleted.model_validate(ended.data)
        copy = call.model_copy(update={"replayed": True})
        await self._publish(site, "tool.call.completed", copy.model_dump(mode="json"))
        return ToolResult(
            tool=name,
            output=as_recorded(call.output),
            provider=call.provider,
            untrusted=call.untrusted,
            latency_ms=call.latency_ms,
        )

    async def _stubbed(
        self, binding: Binding, args: dict[str, Any], site: CallSite, run: str
    ) -> ToolResult:
        """A side effect a fork holds back. Policy still decides, as it would; nobody is asked,
        because nothing happens. The output is the source's here if it made the same call."""
        spec = binding.spec
        decision = await self._decide(spec, args, site)
        await self._record_decision(site, spec.name, decision)
        if decision.effect is Effect.DENY:
            raise ToolDeniedError(f"{spec.name}: {decision.reason or 'denied by policy'}")
        asked, ended = await self._source_call(site, spec.name, run)
        same = (
            ended is not None
            and ended.type == "tool.call.completed"
            and asked == as_source(args, site.event.runid, run)
        )
        output: JsonValue = as_recorded(
            ToolCallCompleted.model_validate(ended.data).output
            if same and ended is not None
            else {"stubbed": True, "tool": spec.name}
        )
        untrusted = not spec.trusted
        await self._publish(
            site,
            "tool.call.completed",
            ToolCallCompleted(
                call=site.index,
                site=site.key("tool"),
                tool=spec.name,
                provider=binding.provider.name,
                latency_ms=0.0,
                output=output,
                untrusted=untrusted,
                stubbed=True,
            ).model_dump(mode="json"),
        )
        return ToolResult(
            tool=spec.name,
            output=output,
            provider=binding.provider.name,
            untrusted=untrusted,
            latency_ms=0.0,
        )

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

    async def _authorize(
        self, spec: ToolSpec, arguments: dict[str, Any], site: CallSite, rationale: str | None
    ) -> None:
        decision = await self._decide(spec, arguments, site)
        refused = None if decision.effect is Effect.DENY else self._refused(spec, arguments, site)
        if refused is not None:
            decision = PolicyDecision(Effect.DENY, ("egress",), reason=refused)
        await self._record_decision(site, spec.name, decision)
        if refused is not None:
            raise EgressDeniedError(f"{spec.name}: {refused}")
        if decision.effect is Effect.DENY:
            raise ToolDeniedError(f"{spec.name}: {decision.reason or 'denied by policy'}")
        if decision.effect is Effect.ALLOW:
            return
        approval = await self.approvals.require(
            Proposal(
                action=spec.name,
                payload=arguments,
                summary=_summary(spec.name, arguments),
                risk=_risk(spec, site.tainted),
                approvers=decision.approvers,
                policies=decision.policies,
                rationale=rationale,
                tainted=site.tainted,
                taint=site.taint,
                timeout=decision.timeout,
                on_timeout=decision.on_timeout,
            ),
            site,
        )
        decision = await self._decide(spec, arguments, site, approved=True)
        await self._record_decision(site, spec.name, decision, approval=approval)
        if not decision.allowed:
            raise ToolDeniedError(f"{spec.name}: {decision.reason or 'denied by policy'}")

    def _refused(self, spec: ToolSpec, arguments: dict[str, Any], site: CallSite) -> str | None:
        """Why this call can't go out, once the App or the tool has a list to check it against."""
        egress = self.egress(site.event.tenantid, site.event.appid, spec.name)
        if not egress.declared:
            return None
        try:
            hosts = [*spec.reaches, *(host_of(url) for url in urls_in(arguments))]
        except AIOSError as exc:
            return str(exc)
        blocked = sorted({host for host in hosts if not egress.permits(host)})
        return f"{', '.join(blocked)} not on the allowlist" if blocked else None

    async def _decide(
        self, spec: ToolSpec, arguments: dict[str, Any], site: CallSite, *, approved: bool = False
    ) -> PolicyDecision:
        delegate = site.event.onbehalfof
        return await self.policy.decide(
            PolicyRequest(
                tenant=site.event.tenantid,
                app=site.event.appid,
                actor=Actor(site.principal),
                action=PolicyAction.TOOL_INVOKE,
                resource=Resource.tool(spec.name, spec.side_effect),
                on_behalf_of=Actor(delegate) if delegate is not None else None,
                tainted=site.tainted,
                approved=approved,
                input=arguments,
            )
        )

    async def _record_decision(
        self, site: CallSite, tool: str, decision: PolicyDecision, *, approval: str | None = None
    ) -> None:
        verb = {
            Effect.ALLOW: "allowed",
            Effect.DENY: "denied",
            Effect.REQUIRE_APPROVAL: "escalated",
        }[decision.effect]
        await self._publish(
            site,
            f"policy.decision.{verb}",
            PolicyDecided(
                action=PolicyAction.TOOL_INVOKE.value,
                resource=tool,
                policies=list(decision.policies),
                approvers=list(decision.approvers),
                reason=decision.reason,
                approval=approval,
            ).model_dump(mode="json"),
        )

    async def _credentials(self, spec: ToolSpec, site: CallSite) -> dict[str, str]:
        found: dict[str, str] = {}
        for name in spec.credentials:
            value = await self.secrets.get(site.event.tenantid, site.event.appid, name)
            if not value:
                raise ToolCredentialError(f"{spec.name} needs the secret {name}, which is not set")
            found[name] = value
        return found

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
        credentials = await self._credentials(binding.spec, site)
        secrets = list(credentials.values())
        try:
            async with asyncio.timeout(binding.spec.timeout_s):
                output = await binding.provider.call(
                    binding.spec.name,
                    arguments,
                    idempotency_key=idempotency_key,
                    credentials=credentials,
                    egress=self.egress(site.event.tenantid, site.event.appid, binding.spec.name),
                )
        except TimeoutError as exc:
            raise ToolError(f"{binding.spec.name} timed out", retryable=True) from exc
        except Exception as exc:
            message = str(exc)
            if any(secret in message for secret in secrets):
                retryable = bool(getattr(exc, "retryable", True))
                masked = cast(str, _masked(message, secrets))
                raise ToolError(f"{binding.spec.name}: {masked}", retryable=retryable) from None
            raise
        output = _masked(output, secrets)
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

    async def _publish(
        self, site: CallSite, type: str, data: dict[str, Any], *, tainted: bool = False
    ) -> None:
        event = site.event.derive(
            type,
            data,
            source=f"aios://{site.event.tenantid}/system/tools",
            principal=site.principal,
            dataclass=site.data_class,
            tainted=site.tainted or tainted,
        )
        await self.bus.publish(event)

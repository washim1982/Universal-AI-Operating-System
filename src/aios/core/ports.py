"""Ports between runtime components (ADR-001: components never import each other).

The Agent Kernel hands agents a ``Context`` whose ``model``, ``tools`` and ``memory`` calls go
through these interfaces; the Model Kernel, Tool OS and Memory OS implement them, and the
access layer wires them together. Every call carries a :class:`CallSite`, which says which
task it belongs to, so that calls are recorded against the run, attributed and budgeted, and
replayed instead of repeated after a crash (NFR-06).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aios.core.catalog import InjectionSuspected, Risk, Usage
from aios.core.envelope import DataClass, Event
from aios.core.errors import ApprovalRequiredError
from aios.core.memory import (
    ContextPack,
    Erasure,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    Retention,
    SearchMode,
    SearchResult,
    Written,
)
from aios.core.policy import OnTimeout

logger = logging.getLogger("aios.core")


@dataclass(frozen=True, slots=True)
class CallSite:
    """Where a model or tool call happens: the task being executed and the call's position."""

    event: Event
    """The event the agent is handling (``task.requested`` or a subscribed fact)."""
    agent: str
    principal: str
    index: int
    """0-based position of this call among the calls of its kind in this execution."""
    step: str | None = None
    item: int | None = None
    loop: int = 0
    attempt: int = 1
    tainted: bool = False
    """The agent's context already contains untrusted content (FR-SEC-09)."""
    taint: tuple[str, ...] = ()
    """Where that content came from, e.g. ``tool:web.page.read`` or ``memory:<item id>``."""
    tags: Mapping[str, str] = field(default_factory=dict[str, str])
    data_class: DataClass = DataClass.PUBLIC
    """The strictest label of what the agent has read so far (FR-SEC-11)."""
    origin: str = ""
    """Set when a component calls on an agent's behalf, so its calls have their own sites. Memory
    OS embeds with this set: a recovered task must not mistake that call for one of its own."""
    executions: tuple[str, ...] = ()
    """Earlier executions of this task, each stopped to wait for an approval, oldest first. What
    they did counts as done here too, so resuming a task repeats nothing."""

    @property
    def task(self) -> str:
        """The task, however many times it has been resumed."""
        return self.executions[0] if self.executions else self.event.id

    def recorded_by(self, event: Event) -> bool:
        """Whether this task caused ``event``, in this execution or an earlier one."""
        return event.causationid == self.event.id or event.causationid in self.executions

    def key(self, kind: Literal["model", "tool", "memory", "emit"]) -> str:
        """A stable identifier of the call site, independent of models, tools and content.

        ``research#2/research-agent/l0a1/model/0`` is the first model call of the research
        agent on the third fan-out item, first loop, first attempt. Cassettes replay by it.
        """
        step = self.step or self.event.type
        item = f"#{self.item}" if self.item is not None else ""
        path = f"{step}{item}/{self.agent}/l{self.loop}a{self.attempt}"
        return f"{path}/{self.origin}{kind}/{self.index}"


# --------------------------------------------------------------------------- models


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant"]
    content: str


class ModelRequest(BaseModel):
    """A provider-neutral model call (FR-MOD-01)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str | None = Field(default=None, description="Routing hint for the model policy")
    system: str | None = None
    messages: list[Message] = Field(min_length=1)
    output_schema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema for structured output"
    )
    output_name: str | None = None
    max_tokens: int = Field(default=16_000, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    data_class: DataClass = DataClass.INTERNAL


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    parsed: JsonValue = None
    provider: str
    model: str
    usage: Usage
    latency_ms: float = 0.0
    stop_reason: str | None = None
    replayed: bool = Field(
        default=False, description="Taken from the run's recorded calls, not executed again"
    )


class EmbeddingRequest(BaseModel):
    """Texts to embed (FR-MOD-01), routed by the model policy like any other call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(default="embedding", description="Routing hint for the model policy")
    texts: list[str] = Field(min_length=1)
    data_class: DataClass = DataClass.INTERNAL


class EmbeddingResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    vectors: list[list[float]] = Field(description="One vector per text, in the same order")
    provider: str
    model: str
    usage: Usage
    latency_ms: float = 0.0


class ModelGateway(ABC):
    @abstractmethod
    async def generate(self, request: ModelRequest, site: CallSite) -> ModelResponse: ...

    @abstractmethod
    async def embed(self, request: EmbeddingRequest, site: CallSite) -> EmbeddingResponse: ...

    async def can_embed(self, request: EmbeddingRequest, site: CallSite) -> bool:
        """Whether a model could serve this request, for callers that can go without one."""
        return True


# --------------------------------------------------------------------------- tools


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    output: JsonValue
    provider: str
    untrusted: bool = Field(default=True, description="Tool output is untrusted data (FR-SEC-09)")
    latency_ms: float = 0.0


class ToolGateway(ABC):
    @abstractmethod
    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, JsonValue],
        site: CallSite,
        *,
        idempotency_key: str | None = None,
        rationale: str | None = None,
    ) -> ToolResult: ...


# --------------------------------------------------------------------------- approvals


@dataclass(frozen=True, slots=True)
class Proposal:
    """An action a policy wants a person to look at first (FR-HUM-03)."""

    action: str
    payload: JsonValue
    summary: str
    risk: Risk
    approvers: tuple[str, ...]
    policies: tuple[str, ...]
    rationale: str | None = None
    tainted: bool = False
    taint: tuple[str, ...] = ()
    timeout: timedelta | None = None
    on_timeout: OnTimeout = "deny"


class ApprovalGateway(ABC):
    @abstractmethod
    async def require(self, proposal: Proposal, site: CallSite) -> str:
        """The id of the approval for exactly this call, if a person has given it.

        Otherwise raises :class:`~aios.core.errors.AwaitingApproval` while someone decides,
        :class:`~aios.core.errors.ApprovalDeniedError` if the answer was no, and
        :class:`~aios.core.errors.ApprovalRequiredError` if nothing here can wait.
        """


class NoApprovals(ApprovalGateway):
    """For components used without Human OS: nobody can approve, so nothing goes ahead."""

    async def require(self, proposal: Proposal, site: CallSite) -> str:
        raise ApprovalRequiredError(f"{proposal.action}: approval required", proposal.approvers)


# --------------------------------------------------------------------------- injection


@dataclass(frozen=True, slots=True)
class Verdict:
    suspected: bool
    score: float = 0.0
    rules: tuple[str, ...] = ()


class InjectionClassifier(ABC):
    """Looks at content before an agent reads it (FR-SEC-09). A signal: nothing relies on it."""

    name: ClassVar[str]

    @abstractmethod
    async def classify(self, text: str, *, source: str) -> Verdict: ...


class NoClassifier(InjectionClassifier):
    """For an installation that has chosen to look at nothing. Taint and policy still apply."""

    name = "none"

    async def classify(self, text: str, *, source: str) -> Verdict:
        return Verdict(False)


async def screened(
    classifier: InjectionClassifier | None, content: object, source: str
) -> InjectionSuspected | None:
    """What to record about ``content``, if anything. A classifier that fails stops nothing."""
    if classifier is None or content is None:
        return None
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    try:
        verdict = await classifier.classify(text, source=source)
    except Exception as exc:
        logger.warning("injection classifier %s failed: %s", classifier.name, exc)
        return None
    if not verdict.suspected:
        return None
    return InjectionSuspected(
        source=source, classifier=classifier.name, score=verdict.score, rules=list(verdict.rules)
    )


class SecretStore(ABC):
    """Where service credentials come from (FR-TOOL-06). Vault backends arrive in WP 2.2."""

    @abstractmethod
    async def get(self, tenant: str, app: str, name: str) -> str | None: ...


class NoSecrets(SecretStore):
    async def get(self, tenant: str, app: str, name: str) -> str | None:
        return None


# --------------------------------------------------------------------------- memory


class MemoryGateway(ABC):
    """What an agent can do with memory (§7.7). Every call is checked against its scopes."""

    @abstractmethod
    async def put(
        self,
        content: str,
        *,
        scope: MemoryScope,
        site: CallSite,
        subject: str | None = None,
        kind: MemoryKind = "semantic",
        retention: Retention | None = None,
        ttl: timedelta | None = None,
        sensitivity: DataClass | None = None,
        importance: float = 0.5,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> Written: ...

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        scopes: Sequence[MemoryScope],
        site: CallSite,
        k: int = 5,
        filters: Mapping[str, Any] | None = None,
        mode: SearchMode = "hybrid",
    ) -> SearchResult: ...

    @abstractmethod
    async def recall(
        self, query: str, *, scopes: Sequence[MemoryScope], budget_tokens: int, site: CallSite
    ) -> ContextPack: ...

    @abstractmethod
    async def get(self, item_id: str, *, site: CallSite) -> MemoryItem | None: ...

    @abstractmethod
    async def update(
        self,
        item_id: str,
        *,
        site: CallSite,
        content: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
        importance: float | None = None,
        version: int | None = None,
    ) -> Written: ...

    @abstractmethod
    async def forget(
        self, *, site: CallSite, item: str | None = None, subject: str | None = None
    ) -> Erasure: ...


class NoMemory(MemoryGateway):
    """For tests of what surrounds memory: it recalls nothing and refuses to store."""

    async def put(
        self,
        content: str,
        *,
        scope: MemoryScope,
        site: CallSite,
        subject: str | None = None,
        kind: MemoryKind = "semantic",
        retention: Retention | None = None,
        ttl: timedelta | None = None,
        sensitivity: DataClass | None = None,
        importance: float = 0.5,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> Written:
        raise NotImplementedError("this worker has no Memory OS")

    async def search(
        self,
        query: str,
        *,
        scopes: Sequence[MemoryScope],
        site: CallSite,
        k: int = 5,
        filters: Mapping[str, Any] | None = None,
        mode: SearchMode = "hybrid",
    ) -> SearchResult:
        return SearchResult()

    async def recall(
        self, query: str, *, scopes: Sequence[MemoryScope], budget_tokens: int, site: CallSite
    ) -> ContextPack:
        return ContextPack()

    async def get(self, item_id: str, *, site: CallSite) -> MemoryItem | None:
        return None

    async def update(
        self,
        item_id: str,
        *,
        site: CallSite,
        content: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
        importance: float | None = None,
        version: int | None = None,
    ) -> Written:
        raise NotImplementedError("this worker has no Memory OS")

    async def forget(
        self, *, site: CallSite, item: str | None = None, subject: str | None = None
    ) -> Erasure:
        return Erasure()

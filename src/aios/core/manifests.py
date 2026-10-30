"""Agent, App, Workflow and Policy manifests, API version ``aios/v1alpha1`` (requirements
§7.3, §7.4, §7.9).

Manifests are YAML documents with Kubernetes-style ``apiVersion`` / ``kind`` / ``metadata`` /
``spec``. YAML keys are camelCase; the Python attributes are snake_case.

The Workflow manifest is the v0.1 draft of the graph IR that the Python DSL (§7.6) compiles to.
It covers the node types the reference apps need first (task, dynamic fan-out, approval, tool);
WP 1.3 adds the rest of FR-WF-02.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    TypeAdapter,
    WithJsonSchema,
    model_validator,
)
from pydantic.alias_generators import to_camel

from aios.core.envelope import DataClass
from aios.core.policy import PolicyAction, SideEffect
from aios.core.topics import (
    DNS_LABEL_PATTERN,
    PATTERN_REGEX,
    TOKEN_PATTERN,
    CapabilityName,
    CapabilityPattern,
    TopicPattern,
    max_match_length,
    min_match_length,
    validate_name,
    validate_pattern,
)

API_VERSION = "aios/v1alpha1"

# --------------------------------------------------------------------------- field types

Name = Annotated[str, StringConstraints(pattern=rf"^{DNS_LABEL_PATTERN}$")]
"""Resource name: lowercase letters, digits and hyphens, at most 63 characters."""

SEMVER_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SemVer = Annotated[str, StringConstraints(pattern=SEMVER_PATTERN)]

_DURATION_RE = re.compile(r"^(\d+)(ms|s|m|h|d)$")
_DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: object) -> timedelta:
    """Accept ``"500ms"``, ``"30s"``, ``"5m"``, ``"24h"``, ``"7d"`` (or a timedelta).

    Bare numbers are rejected, as in the JSON Schema: ``timeout: 30`` does not say its unit.
    """
    if isinstance(value, timedelta):
        return value
    match = _DURATION_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"invalid duration {value!r}; use e.g. 500ms, 30s, 5m, 24h, 7d")
    return timedelta(seconds=int(match[1]) * _DURATION_UNITS[match[2]])


def format_duration(value: timedelta) -> str:
    total_ms = round(value.total_seconds() * 1000)
    for unit in ("d", "h", "m", "s"):
        size = round(_DURATION_UNITS[unit] * 1000)
        if total_ms and total_ms % size == 0:
            return f"{total_ms // size}{unit}"
    return f"{total_ms}ms"


Duration = Annotated[
    timedelta,
    BeforeValidator(parse_duration),
    PlainSerializer(format_duration, return_type=str),
    WithJsonSchema({"type": "string", "pattern": _DURATION_RE.pattern}),
]

TOOL_NAME_TOKENS = 3


def _tool_name(value: str) -> str:
    validate_name(value, "tool name")
    if value.count(".") != TOOL_NAME_TOKENS - 1:
        raise ValueError(f"tool names are <provider>.<resource>.<action>: {value!r}")
    return value


def _tool_pattern(value: str) -> str:
    validate_pattern(value, "tool pattern")
    longest = max_match_length(value)
    too_short = longest is not None and longest < TOOL_NAME_TOKENS
    if too_short or min_match_length(value) > TOOL_NAME_TOKENS:
        # Only a too-short pattern can be fixed by widening its last "*" to ">".
        hint = value.rsplit(".", 1)[0] + ".>" if too_short and value.endswith(".*") else None
        raise ValueError(
            f"tool pattern {value!r} can never match a <provider>.<resource>.<action> name"
            + (f"; did you mean {hint!r}?" if hint else "")
        )
    return value


ToolName = Annotated[
    str,
    AfterValidator(_tool_name),
    WithJsonSchema({"type": "string", "pattern": rf"^{TOKEN_PATTERN}(?:\.{TOKEN_PATTERN}){{2}}$"}),
]
ToolPattern = Annotated[
    str,
    AfterValidator(_tool_pattern),
    WithJsonSchema({"type": "string", "pattern": PATTERN_REGEX}),
]
SecretName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]
Selector = Annotated[str, StringConstraints(pattern=r"^(role|user|group):[A-Za-z0-9._@-]+$")]

MemoryScope = Literal["working", "session", "user", "agent", "app", "tenant", "global"]
Isolation = Literal["inprocess", "process", "container", "sandbox"]


class _Model(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
    )


class Metadata(_Model):
    name: Name
    version: SemVer
    labels: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- Agent


class Runtime(_Model):
    type: Annotated[str, StringConstraints(pattern=r"^(python|container|a2a|adapter:[a-z0-9-]+)$")]
    entrypoint: str | None = Field(default=None, description="python: module.path:function")
    image: str | None = Field(default=None, description="container: OCI image reference")
    url: str | None = Field(default=None, description="a2a: the remote agent's base URL")

    @model_validator(mode="after")
    def _check_type_fields(self) -> Self:
        required = {"python": "entrypoint", "container": "image", "a2a": "url"}.get(self.type)
        if required and getattr(self, required) is None:
            raise ValueError(f"runtime type {self.type!r} requires {required!r}")
        if (
            self.type == "python"
            and self.entrypoint
            and not re.fullmatch(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*", self.entrypoint)
        ):
            raise ValueError(f"python entrypoint must be 'module.path:function': {self.entrypoint}")
        return self


class CapabilityDecl(_Model):
    name: CapabilityName
    description: str = ""
    input: str | None = Field(default=None, description="Path or URI of the input JSON Schema")
    output: str | None = Field(default=None, description="Path or URI of the output JSON Schema")


class ToolAccess(_Model):
    allow: list[ToolPattern] = Field(default_factory=list)
    deny: list[ToolPattern] = Field(default_factory=list)


class MemoryAccess(_Model):
    read: list[MemoryScope] = []
    write: list[MemoryScope] = []


class ModelRequirements(_Model):
    policy: Name = "default"
    task: str | None = Field(default=None, description="Routing hint resolved by the model policy")


class Limits(_Model):
    max_steps: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0, alias="maxCostUSD")
    timeout: Duration | None = None
    concurrency: int = Field(default=1, ge=1)


class Scaling(_Model):
    min: int = Field(default=0, ge=0)
    max: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min > self.max:
            raise ValueError("scaling.min must not exceed scaling.max")
        return self


class AgentSpec(_Model):
    description: str = ""
    runtime: Runtime
    capabilities: list[CapabilityDecl] = Field(min_length=1)
    subscriptions: list[TopicPattern] = Field(default_factory=list)
    tools: ToolAccess = Field(default_factory=ToolAccess)
    memory: MemoryAccess = Field(default_factory=MemoryAccess)
    model: ModelRequirements = Field(default_factory=ModelRequirements)
    limits: Limits = Field(default_factory=Limits)
    scaling: Scaling = Field(default_factory=Scaling)
    isolation: Isolation = "process"


class AgentManifest(_Model):
    model_config = ConfigDict(title="AIOS Agent manifest (aios/v1alpha1)")

    api_version: Literal["aios/v1alpha1"] = API_VERSION
    kind: Literal["Agent"] = "Agent"
    metadata: Metadata
    spec: AgentSpec


# --------------------------------------------------------------------------- App


class ChatEntrypoint(_Model):
    type: Literal["chat"]
    workflow: Name


class EventEntrypoint(_Model):
    type: Literal["event"]
    on: TopicPattern
    workflow: Name


Entrypoint = Annotated[ChatEntrypoint | EventEntrypoint, Field(discriminator="type")]


class ToolRef(_Model):
    provider: Name | None = Field(default=None, description="A tool provider, e.g. an MCP server")
    name: ToolName | None = Field(default=None, description="A single tool")

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.provider is None) == (self.name is None):
            raise ValueError("a tool reference needs exactly one of 'provider' or 'name'")
        return self


class KnowledgeSource(_Model):
    source: str = Field(min_length=1)
    scope: MemoryScope = "app"


class AppMemory(_Model):
    scopes: list[MemoryScope] = []
    knowledge: list[KnowledgeSource] = []


class Permissions(_Model):
    """What the App asks a tenant admin to consent to at install time (FR-APP-03)."""

    tools: list[ToolPattern] = Field(default_factory=list)
    data_classes: list[DataClass] = Field(default_factory=lambda: [DataClass.INTERNAL])


class AppConfig(_Model):
    config_schema: str | None = Field(default=None, alias="schema")
    secrets: list[SecretName] = Field(default_factory=list)


class AppBudgets(_Model):
    per_run_usd: float | None = Field(default=None, ge=0, alias="perRunUSD")
    monthly_usd: float | None = Field(default=None, ge=0, alias="monthlyUSD")


class AppSpec(_Model):
    description: str = ""
    entrypoints: list[Entrypoint] = Field(min_length=1)
    agents: list[Name] = Field(default_factory=list)
    workflows: list[Name] = Field(default_factory=list)
    tools: list[ToolRef] = []
    memory: AppMemory = Field(default_factory=AppMemory)
    permissions: Permissions = Field(default_factory=Permissions)
    policies: list[Name] = Field(default_factory=list)
    model_policy: Name = "default"
    config: AppConfig = Field(default_factory=AppConfig)
    budgets: AppBudgets = Field(default_factory=AppBudgets)

    @model_validator(mode="after")
    def _entrypoints_use_declared_workflows(self) -> Self:
        for entrypoint in self.entrypoints:
            if entrypoint.workflow not in self.workflows:
                raise ValueError(
                    f"entrypoint {entrypoint.type!r} uses workflow {entrypoint.workflow!r}, "
                    "which is not listed in spec.workflows"
                )
        return self


class AppManifest(_Model):
    model_config = ConfigDict(title="AIOS App manifest (aios/v1alpha1)")

    api_version: Literal["aios/v1alpha1"] = API_VERSION
    kind: Literal["App"] = "App"
    metadata: Metadata
    spec: AppSpec


# --------------------------------------------------------------------------- Workflow

StepId = Annotated[str, StringConstraints(pattern=rf"^{DNS_LABEL_PATTERN}$")]


class _StepBase(_Model):
    id: StepId
    needs: list[StepId] | None = Field(
        None, description="Steps that must finish first; defaults to the previous step"
    )
    timeout: Duration | None = None
    max_attempts: int = Field(default=3, ge=1)


class TaskStep(_StepBase):
    type: Literal["task"]
    capability: CapabilityPattern
    input: dict[str, Any] = Field(default_factory=dict, description="Input bindings")
    on_result: dict[str, StepId] = Field(
        default_factory=dict, description="Route a result value back to an earlier step"
    )
    max_loops: int = Field(default=0, ge=0, description="Bound on on_result back-edges")


class DynamicStep(_StepBase):
    """Planner-driven fan-out: one task per item in an earlier step's output (FR-WF-03)."""

    type: Literal["dynamic"]
    capability: CapabilityPattern
    from_plan: str = Field(
        min_length=1, description="<earlier step id>.output.<path>, e.g. plan.output.tasks"
    )
    max_parallel: int = Field(default=5, ge=1)


class ApprovalStep(_StepBase):
    type: Literal["approval"]
    approvers: list[Selector] = Field(min_length=1)
    on_timeout: Literal["deny", "approve", "escalate"] = "deny"


class ToolStep(_StepBase):
    type: Literal["tool"]
    tool: ToolName
    input: dict[str, Any] = Field(default_factory=dict)


Step = Annotated[TaskStep | DynamicStep | ApprovalStep | ToolStep, Field(discriminator="type")]


class WorkflowBudget(_Model):
    max_cost_usd: float | None = Field(default=None, ge=0, alias="maxCostUSD")
    max_duration: Duration | None = None
    max_steps: int | None = Field(default=None, ge=1)


_STEP_REFERENCE = re.compile(r"\$\{(?!input\b)([a-z0-9-]+)\.output\b[^}]*\}")


class WorkflowSpec(_Model):
    description: str = ""
    steps: list[Step] = Field(min_length=1)
    output: str | None = Field(
        default=None,
        description="What the run returns, e.g. ${draft.output} (default: the last step's output)",
    )
    budget: WorkflowBudget = Field(default_factory=WorkflowBudget)

    @model_validator(mode="after")
    def _check_graph(self) -> Self:
        position: dict[str, int] = {}
        for index, step in enumerate(self.steps):
            if step.id in position:
                raise ValueError(f"duplicate step id {step.id!r}")
            position[step.id] = index
        for index, step in enumerate(self.steps):
            _check_step(step, index, position)
        for referenced in _STEP_REFERENCE.findall(self.output or ""):
            if referenced not in position:
                raise ValueError(f"workflow output reads unknown step {referenced!r}")
        return self


def _check_step(step: Step, index: int, position: dict[str, int]) -> None:
    def earlier(step_id: str) -> bool:
        return step_id in position and position[step_id] < index

    # `needs` may only point backwards, so the forward graph is acyclic by construction;
    # loops are explicit, bounded on_result back-edges.
    for dependency in step.needs or []:
        if dependency not in position:
            raise ValueError(f"step {step.id!r} needs unknown step {dependency!r}")
        if not earlier(dependency):
            raise ValueError(f"step {step.id!r} needs later step {dependency!r}")
    if isinstance(step, TaskStep):
        if not all(earlier(target) for target in step.on_result.values()):
            raise ValueError(f"step {step.id!r} on_result must target an earlier step")
        if step.on_result and step.max_loops == 0:
            raise ValueError(f"step {step.id!r} has on_result but max_loops is 0")
    for referenced in _STEP_REFERENCE.findall(str(getattr(step, "input", ""))):
        if not earlier(referenced):
            raise ValueError(f"step {step.id!r} input reads unknown or later {referenced!r}")
    if isinstance(step, DynamicStep) and not earlier(step.from_plan.split(".", 1)[0]):
        raise ValueError(
            f"step {step.id!r} from_plan must read an earlier step: {step.from_plan!r}"
        )


class WorkflowManifest(_Model):
    model_config = ConfigDict(title="AIOS Workflow manifest (aios/v1alpha1)")

    api_version: Literal["aios/v1alpha1"] = API_VERSION
    kind: Literal["Workflow"] = "Workflow"
    metadata: Metadata
    spec: WorkflowSpec


# --------------------------------------------------------------------------- Policy

# Cedar reserves these identifiers, so they cannot be used as attribute names after a dot.
_CEDAR_RESERVED = frozenset(
    {"true", "false", "if", "then", "else", "in", "is", "like", "has", "__cedar"}
)


def _input_field(value: str) -> str:
    if value in _CEDAR_RESERVED:
        raise ValueError(f"{value!r} is reserved and cannot be an input field name")
    return value


InputField = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"), AfterValidator(_input_field)
]


class Comparison(_Model):
    """One comparison against a tool-input field. Numbers compare with 4 decimal places."""

    gt: float | None = None
    gte: float | None = None
    lt: float | None = None
    lte: float | None = None
    eq: float | str | bool | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if len(self._given()) != 1:
            raise ValueError("a comparison needs exactly one of gt, gte, lt, lte, eq")
        return self

    def _given(self) -> list[tuple[str, float | str | bool]]:
        values = (("gt", self.gt), ("gte", self.gte), ("lt", self.lt), ("lte", self.lte))
        given = [(name, value) for name, value in values if value is not None]
        return given + ([("eq", self.eq)] if self.eq is not None else [])

    @property
    def operator(self) -> str:
        return self._given()[0][0]

    @property
    def value(self) -> float | str | bool:
        return self._given()[0][1]


class Condition(_Model):
    """When a rule applies. Every listed condition must hold."""

    side_effect: list[SideEffect] | None = Field(
        default=None, min_length=1, description="The tool's side-effect class is one of these"
    )
    context_tainted: bool | None = Field(
        default=None, description="The run's context does (true) or doesn't contain untrusted data"
    )
    input: dict[InputField, Comparison] = Field(
        default_factory=dict, description="Comparisons against top-level tool-input fields"
    )


class PolicyRule(_Model):
    action: PolicyAction
    resource: ToolPattern | None = Field(
        default=None, description="tool.invoke only: a tool name or pattern; omit for every tool"
    )
    scopes: list[MemoryScope] | None = Field(
        default=None, min_length=1, description="memory.read / memory.write only"
    )
    when: Condition = Field(default_factory=Condition)

    @model_validator(mode="after")
    def _fields_fit_the_action(self) -> Self:
        if self.action is PolicyAction.TOOL_INVOKE:
            if self.scopes is not None:
                raise ValueError("tool.invoke rules use 'resource', not 'scopes'")
        else:
            if self.scopes is None:
                raise ValueError(f"{self.action.value} rules need 'scopes'")
            if self.resource is not None:
                raise ValueError(f"{self.action.value} rules use 'scopes', not 'resource'")
            if self.when.side_effect is not None or self.when.input:
                raise ValueError("sideEffect and input conditions apply to tool.invoke only")
        return self


class ApprovalRule(PolicyRule):
    approvers: list[Selector] = Field(min_length=1)


class PolicySubject(_Model):
    """Whom the policy is about: exactly one agent, app, role or user."""

    agent: Name | None = None
    app: Name | None = None
    role: Name | None = None
    user: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._~-]+$")] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if len(self._given()) != 1:
            raise ValueError("a policy subject needs exactly one of agent, app, role or user")
        return self

    def _given(self) -> list[tuple[str, str]]:
        values = (
            ("agent", self.agent),
            ("app", self.app),
            ("role", self.role),
            ("user", self.user),
        )
        return [(kind, name) for kind, name in values if name is not None]

    @property
    def kind(self) -> str:
        return self._given()[0][0]

    @property
    def name(self) -> str:
        return self._given()[0][1]


class PolicySpec(_Model):
    description: str = ""
    subject: PolicySubject
    allow: list[PolicyRule] = []
    deny: list[PolicyRule] = []
    require_approval: list[ApprovalRule] = []

    @model_validator(mode="after")
    def _has_rules(self) -> Self:
        if not (self.allow or self.deny or self.require_approval):
            raise ValueError("a policy needs at least one allow, deny or requireApproval rule")
        return self


class PolicyManifest(_Model):
    """Requirements §7.9. Compiled to Cedar by :mod:`aios.security.cedar` (ADR-007)."""

    model_config = ConfigDict(title="AIOS Policy manifest (aios/v1alpha1)")

    api_version: Literal["aios/v1alpha1"] = API_VERSION
    kind: Literal["Policy"] = "Policy"
    metadata: Metadata
    spec: PolicySpec


# --------------------------------------------------------------------------- ModelPolicy

Candidate = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9-]+/[A-Za-z0-9._:<>-]+$"),
    WithJsonSchema({"type": "string", "pattern": r"^[a-z0-9-]+/[A-Za-z0-9._:<>-]+$"}),
]
"""``<provider>/<model>``; ``<…>`` placeholders are filled from the model registry."""


class ModelMatch(_Model):
    task: str | None = None
    data_class: DataClass | None = None
    agent: Name | None = None


class ModelRequirement(_Model):
    deployment: Literal["public", "private"] | None = None


class ModelRule(_Model):
    match: ModelMatch = Field(default_factory=ModelMatch)
    require: ModelRequirement = Field(default_factory=ModelRequirement)
    candidates: list[Candidate] = Field(min_length=1)


class ModelDefault(_Model):
    candidates: list[Candidate] = Field(min_length=1)


class ModelRetries(_Model):
    max: int = Field(default=2, ge=0)
    backoff: Literal["exponential", "none"] = "exponential"


class ModelBudgets(_Model):
    per_run_usd: float | None = Field(default=None, ge=0, alias="perRunUSD")
    per_tenant_monthly_usd: float | None = Field(default=None, ge=0, alias="perTenantMonthlyUSD")
    on_soft_limit: Literal["downgrade", "warn", "deny"] = "warn"


class ModelPolicySpec(_Model):
    rules: list[ModelRule] = []
    default: ModelDefault
    fallback: Literal["next-candidate", "none"] = "next-candidate"
    retries: ModelRetries = Field(default_factory=ModelRetries)
    budgets: ModelBudgets = Field(default_factory=ModelBudgets)


class ModelPolicyManifest(_Model):
    """Requirements §7.10: declarative model routing (FR-MOD-03), read by the Model Kernel."""

    model_config = ConfigDict(title="AIOS ModelPolicy manifest (aios/v1alpha1)")

    api_version: Literal["aios/v1alpha1"] = API_VERSION
    kind: Literal["ModelPolicy"] = "ModelPolicy"
    metadata: Metadata
    spec: ModelPolicySpec


# --------------------------------------------------------------------------- loading

AnyManifest = AgentManifest | AppManifest | WorkflowManifest | PolicyManifest | ModelPolicyManifest
Manifest = Annotated[AnyManifest, Field(discriminator="kind")]
_MANIFEST = TypeAdapter[AnyManifest](Manifest)

_PLACEHOLDER = re.compile(r"\$\{config\.([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate_config(value: Any, config: Mapping[str, Any]) -> Any:
    """Replace ``${config.<key>}`` placeholders with App configuration values.

    A string that is exactly one placeholder takes the value with its type (so a threshold stays
    a number); placeholders inside a longer string are replaced by text.
    """
    if isinstance(value, dict):
        return {k: interpolate_config(v, config) for k, v in cast(dict[str, Any], value).items()}
    if isinstance(value, list):
        return [interpolate_config(item, config) for item in cast(list[Any], value)]
    if not isinstance(value, str):
        return value

    def lookup(match: re.Match[str]) -> Any:
        if match[1] not in config:
            raise ValueError(f"no config value for {match[0]}")
        return config[match[1]]

    whole = _PLACEHOLDER.fullmatch(value)
    if whole is not None:
        return lookup(whole)
    return _PLACEHOLDER.sub(lambda match: str(lookup(match)), value)


class _Yaml12Loader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans: only true/false, so keys like ``on:`` stay strings."""


_BOOL_TAG = "tag:yaml.org,2002:bool"
_Yaml12Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != _BOOL_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(  # pyright: ignore[reportUnknownMemberType]
    _BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def parse_manifest(document: dict[str, Any]) -> AnyManifest:
    """Validate one manifest document, choosing the model by its ``kind``."""
    return _MANIFEST.validate_python(document)


def load_manifests(
    source: str | Path, *, config: Mapping[str, Any] | None = None
) -> list[AnyManifest]:
    """Load every manifest in a YAML file or string (multi-document YAML is supported).

    With ``config``, ``${config.<key>}`` placeholders are resolved first (see
    :func:`interpolate_config`).
    """
    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
    documents: list[Any] = [
        doc
        for doc in yaml.load_all(text, Loader=_Yaml12Loader)  # a SafeLoader subclass
        if doc is not None
    ]
    for doc in documents:
        if not isinstance(doc, dict):
            raise ValueError("each YAML document must be a mapping")
    if config is not None:
        documents = [interpolate_config(doc, config) for doc in documents]
    return [parse_manifest(doc) for doc in documents]

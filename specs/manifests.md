# Manifests `aios/v1alpha1`

Agent, App, Workflow, Policy and ModelPolicy resources share the Kubernetes shape `apiVersion` / `kind` /
`metadata` / `spec`. Python models: `aios.core.manifests`. Generated schemas:
[agent](schemas/manifests/agent.v1alpha1.json), [app](schemas/manifests/app.v1alpha1.json),
[workflow](schemas/manifests/workflow.v1alpha1.json),
[policy](schemas/manifests/policy.v1alpha1.json),
[model-policy](schemas/manifests/model-policy.v1alpha1.json). Worked examples:
[apps/ai-researcher](../apps/ai-researcher/manifests/), [apps/ai-support-desk](../apps/ai-support-desk/manifests/).

## Common rules

- `metadata.name`: DNS label (lowercase letters, digits, hyphens; ≤ 63). `metadata.version`:
  SemVer 2.0. `metadata.labels`: string map.
- YAML keys are camelCase (`maxSteps`, `maxCostUSD`, `fromPlan`). Unknown keys are rejected.
- Durations are strings: `500ms`, `30s`, `5m`, `24h`, `7d`.
- YAML is read with **YAML 1.2 booleans** — only `true`/`false` — so keys such as `on:` stay
  strings. Multi-document files (`---`) are supported.

## Agent (requirements §7.3, FR-AGT-01)

| Field | Rule |
|---|---|
| `spec.runtime.type` | `python` (needs `entrypoint: module.path:function`), `container` (needs `image`), `a2a` (needs `url`), or `adapter:<name>` |
| `spec.capabilities` | At least one; each `name` is a capability name (no wildcards) with optional `input` / `output` schema references |
| `spec.subscriptions` | Topic patterns for side reactions; normal work arrives by capability |
| `spec.tools.allow` / `deny` | Tool patterns; must be able to match a three-token tool name |
| `spec.memory.read` / `write` | Memory scopes: `working`, `session`, `user`, `agent`, `app`, `tenant`, `global` |
| `spec.model` | `policy` name and a routing `task` hint — never a model name |
| `spec.limits` | `maxSteps`, `maxTokens`, `maxCostUSD`, `timeout`, `concurrency` (default 1) |
| `spec.scaling` | `min` ≤ `max` |
| `spec.isolation` | `inprocess`, `process` (default), `container`, `sandbox` |

## App (requirements §7.4, FR-APP-01)

| Field | Rule |
|---|---|
| `spec.entrypoints` | At least one: `chat` (`workflow`; the run's input is `{message}`) or `event` (`on`: topic pattern, `workflow`; each matching fact starts one run whose input is the fact's data, and a redelivered fact never starts a second). Schedules, webhooks, OpenAI-compatible and A2A entry points arrive in v1.0 (FR-APP-02). Each workflow must be listed in `spec.workflows`. |
| `spec.agents`, `spec.workflows`, `spec.policies` | Resource names |
| `spec.tools` | Each item has exactly one of `provider` (e.g. an MCP server) or `name` (one tool) |
| `spec.memory` | `scopes` and `knowledge` sources (`source` URI, `scope`) |
| `spec.permissions` | What the tenant admin consents to at install (FR-APP-03): `tools` patterns and `dataClasses` |
| `spec.config` | `schema` (config JSON Schema reference) and `secrets` (`UPPER_SNAKE` names) |
| `spec.budgets` | `perRunUSD`, `monthlyUSD` |

## Workflow (graph IR v0.1, FR-WF-01)

The Python DSL (§7.6) compiles to this. v0.1 covers the node types the first two reference
apps need; WP 1.3 adds conditions, joins, sub-workflows, waits and compensation (FR-WF-02).

| Step `type` | Fields |
|---|---|
| `task` | `capability` (name or pattern), `input` bindings, `onResult` (result value → earlier step id), `maxLoops` |
| `dynamic` | Planner-driven fan-out (FR-WF-03): `capability`, `fromPlan` (`<earlier step id>.output.<path>`, e.g. `plan.output.tasks`), `maxParallel` |
| `approval` | `approvers` (`role:`, `user:` or `group:` selectors), `onTimeout`: `deny` (default), `approve`, `escalate` |
| `tool` | `tool` (three-token tool name), `input` |

Every step has `id` (DNS label, unique), `needs` (defaults to the previous step), `timeout` and
`maxAttempts` (default 3). `needs` may only point to earlier steps, so the forward graph is
acyclic by construction; loops are explicit `onResult` back-edges bounded by `maxLoops`.
`spec.budget` sets `maxCostUSD`, `maxDuration` and `maxSteps` for the whole run.

Step `input` bindings and `spec.output` use expressions: `${input}`, `${input.<path>}`,
`${<step>.output}` and `${<step>.output.<path>}`. A value that is exactly one expression keeps
its type; expressions inside a longer string become text. Without `input`, a step receives the
output of its single `needs` step (a map of outputs for several, the run's input for the first
step); a step re-run by an `onResult` loop also receives the triggering output as `feedback`.
`spec.output` says what the run returns (default: the last step's output). A run finishes on
the workflow version it started with, even if a newer version is installed meanwhile.

## Policy (requirements §7.9, FR-SEC-03, ADR-007)

Compiled to Cedar by `aios.security.cedar` and validated against
[policy/aios.cedarschema](policy/aios.cedarschema). Anything no rule allows is denied; `deny`
wins over `allow`; `requireApproval` turns an allowed action into one that waits for an
approver (FR-HUM-01) and never allows what no `allow` rule allows.

| Field | Rule |
|---|---|
| `spec.subject` | Exactly one of `agent`, `app` (every agent of the App), `role` or `user` |
| `spec.allow`, `spec.deny` | Rules: `action` plus a target and an optional `when` |
| `spec.requireApproval` | Rules with `approvers` (`role:`, `user:` or `group:` selectors) |
| `action` | v0.1: `tool.invoke`, `memory.read`, `memory.write` (WP 2.2 adds the other enforcement points) |
| `resource` | `tool.invoke` only: a tool name or pattern (`zendesk.>`, `filesystem.*.delete`); omitted means every tool |
| `scopes` | `memory.*` only: memory scopes |
| `when.sideEffect` | The tool's side-effect class is one of `read`, `write`, `destructive`, `external_comm`, `financial`, `code_exec` |
| `when.contextTainted` | The run's context does (`true`) or does not (`false`) contain untrusted content (FR-SEC-09) |
| `when.input` | Comparisons against top-level tool-input fields: `{amountUSD: {gt: 100}}`. Numbers support `gt`, `gte`, `lt`, `lte`, `eq` (at most 4 decimal places); strings and booleans `eq` |

All `when` conditions must hold. `${config.<key>}` placeholders are filled from the App
configuration at install time; a value that is exactly one placeholder keeps its type, so a
threshold stays a number. An agent acting for a user gets the intersection of both decisions
(FR-SEC-05). Requests that can't be evaluated — a string where a number is compared, an unknown
principal kind, a tool without a side-effect class — are denied.

## ModelPolicy (requirements §7.10, FR-MOD-03/04, ADR-008)

Read by the Model Kernel; an App picks one with `spec.modelPolicy` (default `default`, shipped
in `aios/models/defaults/model-policy.yaml`). Candidates are `<provider>/<model>` ids from the
model registry (`aios/models/defaults/registry.yaml`); `<…>` placeholders stand in for models
a deployment fills in.

| Field | Rule |
|---|---|
| `spec.rules[]` | `match` (`task`, `dataClass`, `agent`), optional `require.deployment` (`private`), `candidates` in order |
| `spec.default` | `candidates` when no rule matches |
| `spec.fallback` | `next-candidate` (default) or `none` |
| `spec.retries` | `max` (default 2) and `backoff` (`exponential` or `none`) for retryable errors |
| `spec.budgets` | `perRunUSD`, `perTenantMonthlyUSD`, `onSoftLimit` (enforced from WP 2.6) |

The first matching rule decides. Whatever the rules say, `restricted` data only goes to
`private` deployments (FR-SEC-11).

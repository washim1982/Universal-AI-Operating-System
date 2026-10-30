# ADR-007: Policy engine — Cedar

- **Status:** Accepted — 29 Oct 2026, project owner (requirements Q4, WP 0.4)
- **Date:** 2026-10-28 (proposed), 2026-10-29 (decided)
- **Deciders:** Project owner, tech lead, security engineer
- **Requirements:** FR-SEC-03, 04, 05, 09; FR-HUM-01; NFR-08; principle P4

## Context

Every governed action — publish, subscribe, dispatch, tool call, memory access, model routing,
approvals, admin — passes a policy decision (default deny). AIOS policies are authored in YAML
(requirements §7.9) and compiled to an engine; we should not invent a policy language. Phase 1
needs the enforcement *hooks* in a shape that fits the chosen engine.

## Decision

**Cedar**, embedded in-process through the `cedarpy` binding, behind a
`PolicyDecisionPoint` port in `aios.core`.

- The AIOS authorization question is exactly Cedar's model: principal / action / resource /
  context, with agent ∩ user delegation, resource attributes (tool side-effect class) and
  context (taint, approvals, tool input).
- Policies are validated against a schema at compile time, and Cedar's analysis tools can
  later warn admins about over-broad grants at App install time (FR-APP-03).
- OPA remains possible for customers who mandate it: the port keeps the engine swappable.

How AIOS policies map to Cedar (implemented in `aios.security.cedar`):

| AIOS | Cedar |
|---|---|
| `subject: {agent \| user}` / `{app \| role}` | `principal == Agent::"…"` / `principal in App::"…"` (agents are members of their App and roles) |
| `allow` / `deny` | `permit` / `forbid`; forbid wins; anything else is denied by default |
| Tool patterns (`zendesk.>`, `filesystem.*.delete`) | Equality checks on the `Tool` entity's `provider` / `object` / `verb` attributes, so wildcards keep the topic-grammar semantics exactly (`like` would let `*` span tokens) |
| `requireApproval` | `forbid … unless { context.approved }`; a request denied only by such rules, and allowed once approved, returns *require approval* with the rule's approvers (FR-HUM-01) |
| `when: {sideEffect, contextTainted, input}` | `resource.sideEffect`, `context.tainted`, typed `context.input` fields (numbers as Cedar decimals) |
| Delegation (FR-SEC-05) | Evaluated for the agent and for the user; both must allow |
| `${config.*}` | Resolved from the App configuration before compiling |

The engine fails closed: evaluation errors, requests that don't match the schema, and tool
inputs that can't be encoded (a string where a number is compared, more than 4 decimal
places) are denied. Only input fields that policies reference are passed to Cedar.

## Options considered

| | **Cedar** (chosen) | **OPA / Rego** |
|---|---|---|
| Model | Principal / action / resource / context — matches the AIOS request shape directly | General-purpose; any JSON input |
| Analysis | Formally verified evaluator; policy validation against a schema; can answer "can anyone ever do X?" | Testing via Rego unit tests; no equivalent static analysis |
| Embedding in Python | Rust core; `cedarpy` binding (community-maintained) | Sidecar/daemon over HTTP, or compiled to WASM and evaluated in-process |
| Familiarity | Newer; AWS Verified Permissions | Widely known in Kubernetes teams (Gatekeeper) |
| Latency | In-process, ~0.1–0.2 ms per decision (spike) | In-process via WASM, or a network hop to a sidecar |

## Spike results (29 Oct 2026)

Details in [spikes/README.md](../../spikes/README.md#adr-007-cedar-from-python-29-oct-2026).

1. **Compiles.** The requirements §7.9 example and the Support Desk policy compile to Cedar and
   validate against the AIOS Cedar schema; 34 tests cover allow, deny, default deny,
   approvals, taint escalation, delegation, exact wildcards and fail-closed inputs.
2. **Hot path.** On a Windows laptop, a decision takes 0.20 ms at p50 and 1.06 ms at p99 over a
   mixed workload (3,900 decisions/s on one core), 0.11 ms for a single allow. Most of it is
   entity parsing and request validation inside Cedar; caching the per-tenant entity graph
   roughly halves it (WP 2.2).
3. **Binding.** `cedarpy` 4.12.2 is Apache-2.0, had 10 releases in 2026 tracking Cedar 4.x, and
   ships wheels for CPython 3.10–3.14 on Linux, macOS and Windows. It is community-maintained:
   if it stalls, a thin PyO3 wrapper over the `cedar-policy` crate replaces it inside
   `aios.security.cedar` without touching callers.

## Consequences

- `PolicyDecisionPoint.decide(PolicyRequest) -> PolicyDecision` lives in `aios.core.policy`
  now. Phase 1 calls it at every enforcement point with `AllowAll`; WP 2.2 swaps in
  `CedarDecisionPoint` without touching call sites.
- Policy actions are limited to `tool.invoke`, `memory.read` and `memory.write` in v0.1; WP 2.2
  adds publish, subscribe, dispatch, model routing, approvals and admin.
- Cedar is a runtime dependency of the Security Kernel (the `policy` extra until WP 2.2 makes it
  a default dependency).

## Implementation

`src/aios/core/policy.py` (port, request and decision types, `AllowAll`),
`src/aios/core/manifests.py` (`PolicyManifest`, `interpolate_config`),
`src/aios/security/cedar.py` (compiler, Cedar schema, `CedarDecisionPoint`),
`tests/unit/security/test_cedar.py`, `benchmarks/policy_bench.py`. Generated specs:
`specs/schemas/manifests/policy.v1alpha1.json`, `specs/policy/aios.cedarschema`.

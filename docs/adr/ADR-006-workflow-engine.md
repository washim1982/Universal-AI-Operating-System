# ADR-006: Event-sourced graph workflow engine, no deterministic code replay

- **Status:** Accepted — 29 Oct 2026, project owner (WP 0.4)
- **Date:** 2026-10-28
- **Deciders:** Tech lead
- **Requirements:** FR-WF-01–05, FR-WF-14; NFR-06; risk R2

## Context

Runs must survive crashes without repeating completed work (M1 exit criterion 2), support
planner-driven fan-out, bounded loops, approvals that wait for days, and replay/fork from any
step. Temporal-style engines achieve durability by replaying workflow *code* deterministically,
which forbids model calls, clocks and randomness inside workflow code — awkward for agent
planners whose output decides the graph.

## Decision

- Workflows are **data**: a versioned graph IR (`kind: Workflow`, `aios/v1alpha1`) that the
  Python DSL compiles to. The engine interprets the graph; user code runs only inside steps.
- **Run state is a fold over `run.*` / `step.*` / `task.*` events.** Every transition is an
  event appended through the fabric's transactional publish; a snapshot every N events bounds
  recovery time.
- **Step outputs are recorded**, so recovery never re-executes a completed step and replay/fork
  starts from recorded outputs (FR-OBS-05).
- **Dynamic planning** (FR-WF-03): a planner step's output can expand into a fan-out or a
  sub-graph after validation against schema, allowed capabilities, policy and remaining budget.
- **Loops are explicit back-edges** with `maxLoops`; `needs` only points backwards, so the
  forward graph is a DAG by construction (already enforced by the IR schema).
- **Durable timers and waits** are rows the engine polls (approvals, `timeout`, schedules).
- A **Temporal adapter** remains an option for teams that standardize on it (FR-WF-14, P2).

## Alternatives considered

| Option | Why not |
|---|---|
| Temporal as the engine | Deterministic-replay constraints clash with model-driven graphs; a second stateful cluster for the MVP; still possible as an adapter |
| LangGraph as the engine | In-process graph runtime; durability and multi-tenancy are not its focus; host it as an agent runtime instead (FR-INT-04) |
| Pure choreography (no engine) | No owner per run; traces and budgets become guesswork at 1,000 agents (principle P2) |

## Consequences

- Workflow definitions are versioned; in-flight runs finish on the version they started with
  (FR-APP-04).
- The engine is the most complex component (risk R2): it gets property-based tests over the
  run state machine and the kill test in CI from Phase 1.

## Implementation

Built in WP 1.3 (M1): `src/aios/workflow/` — the DSL (`dsl.py`), expression bindings
(`expressions.py`), the run-state fold (`state.py`) and the engine (`engine.py`). IR schema v0.1:
`aios.core.manifests.WorkflowManifest`.

- The state is rebuilt from the log for every trigger and each decision is published as one
  atomic batch, so recovery needs no special code; a redelivered trigger whose effects are in
  the log is skipped. Tested by killing the engine mid-run (`tests/unit/workflow`) and the
  worker mid-fan-out (`tests/e2e/test_ai_researcher.py`).
- One readiness rule — a step (re)runs when one of its inputs completed after the step last
  started — covers sequencing, fan-in and bounded `onResult` loops.
- Durable timers (step timeouts, run duration) are derived from the log and rebuilt on start.
- In-flight runs finish on the workflow version they started with (FR-WF-09).
- Not yet done: snapshots every N events (the fold is O(run length) per trigger), signals and
  queries, manual pause/resume, `on_error` edges and sub-graph expansion from a planner.

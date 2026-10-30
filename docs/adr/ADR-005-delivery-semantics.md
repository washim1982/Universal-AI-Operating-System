# ADR-005: At-least-once delivery, idempotent consumers, effectively-once side effects

- **Status:** Accepted — 29 Oct 2026, project owner (WP 0.4); implemented by both adapters and the conformance suite
- **Date:** 2026-10-28
- **Deciders:** Tech lead
- **Requirements:** FR-EVT-02, 05, 06, 08, 09, 12; NFR-05, NFR-06

## Context

Exactly-once delivery across a broker, workers and external tools is not achievable in
general. What users need is that no event is lost, that work is not visibly repeated, and that
side effects such as refunds happen once.

## Decision

- **At-least-once delivery.** A delivery is leased to one subscriber for `ack_wait` (default
  30 s); unsettled deliveries are redelivered. `extend()` renews a lease for long work.
- **Consumer groups** are the unit of competition: subscribers with the same group name in the
  same scope compete; each group receives every matching event. Groups are durable. A
  subscription without a group is a private ephemeral group (broadcast). Commands are consumed
  through groups ("exactly one handler"); the SDK only registers command handlers that way.
- **Per-run ordering** inside a group: events with the same `runid` are delivered one at a time
  in publish order, including across retries. Events without `runid` are unordered.
- **Retries:** nack → redelivery after capped exponential backoff with equal jitter (defaults:
  0.5 s initial, ×2, 60 s cap, 5 deliveries) → dead letter + `system.dlq.added`. Dead letters
  can be inspected, redriven (fresh count) or discarded, within the caller's scope.
- **Idempotent consumers:** the SDK keeps an inbox keyed by event id (and `idempotencykey` for
  commands) and skips duplicates (FR-EVT-06, WP 1.2).
- **Effectively-once side effects:** write tools take an idempotency key derived from
  `(runid, step, call index)`; providers that support keys get them passed through; others are
  wrapped with a recorded-result check (NFR-06, WP 1.5).
- **Request/reply** is a helper on the port: publish a command, wait for the first
  `<base>.completed` / `<base>.failed` whose `causationid` is the command id.
- **Hop limit:** publishes with `hopcount` > 64 (configurable) are rejected with
  `budget.exceeded`; batches are atomic, so nothing in the batch is stored.

## Alternatives considered

| Option | Why not |
|---|---|
| At-most-once | Loses work on crashes; violates NFR-05 |
| Broker "exactly-once" features | Backend-specific, and they stop at the broker boundary; tool side effects still need keys |
| Global ordering | Kills parallelism; per-run order is what workflows need |

## Consequences

- Every handler must tolerate redelivery; the SDK makes this the default.
- A poison event blocks later events of the same run in that group until it is dead-lettered —
  the price of per-run ordering, bounded by `max_deliveries`.

## Implementation

`src/aios/fabric/port.py` (contract, request/reply), `src/aios/fabric/memory.py`,
`src/aios/fabric/postgres.py`, and `src/aios/testing/fabric.py` (37 conformance cases every
adapter passes). Idempotent consumers are implemented by checking the run's log rather than a
separate inbox table: the kernel skips a task whose `task.completed` / `task.failed` is already
recorded, the engine skips a trigger whose effects are recorded, and the Model Kernel and Tool
OS return a call recorded at the same call site instead of making it again (NFR-06). Write
tools receive an idempotency key derived from `(run, step, attempt, name)`.

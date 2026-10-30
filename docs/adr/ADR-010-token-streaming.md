# ADR-010: Token streams travel outside the fabric

- **Status:** Accepted — 29 Oct 2026, project owner (WP 0.4)
- **Date:** 2026-10-28
- **Deciders:** Tech lead, full-stack engineer
- **Requirements:** FR-EVT-17, FR-UI-01, NFR-01, NFR-03

## Context

Users expect model output to stream token by token. Publishing every token as an event would
multiply event volume by two to three orders of magnitude, swamp the PostgreSQL fabric and the
audit trail, and add latency to every token.

## Decision

- Tokens stream over a **separate channel**: the Model Kernel writes chunks to a per-call
  in-process (or Redis/NATS core, from Phase 4) stream; the API exposes it to clients as
  **SSE** (default) or **WebSocket**, addressed by `runid` and call id, authorized like the run.
- The fabric carries only **summary events**: `model.call.completed` / `model.call.failed`
  with model, usage, cost, latency and a reference to the recorded output.
- The recorded output (the full completion) is stored with the run for replay, so nothing
  that matters for durability or audit depends on the stream.
- Losing a stream (client disconnect, worker crash) never fails the run; clients reconnect and
  receive the final output from the run record.

## Alternatives considered

| Option | Why not |
|---|---|
| Tokens as events | Volume, latency and audit noise; violates NFR-01 headroom |
| No streaming | Poor UX for chat entry points |

## Consequences

- Two delivery paths to test: the event path (durable) and the stream path (best effort).
- The stream channel must enforce the same tenant isolation as the fabric (WP 2.2 isolation
  suite covers it).

## Implementation

WP 1.4 (Model Kernel streaming) and WP 1.7 (API SSE endpoint).

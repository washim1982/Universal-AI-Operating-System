# ADR-002: CloudEvents 1.0 envelope with AIOS extensions

- **Status:** Accepted — 29 Oct 2026, project owner (WP 0.4); implemented in `aios.core.envelope`
- **Date:** 2026-10-28
- **Deciders:** Tech lead
- **Requirements:** FR-EVT-01, FR-EVT-06, FR-EVT-11, FR-SEC-05, FR-SEC-11, FR-OBS-01

## Context

Every component, adapter, SDK and external integration will read and write events. The
envelope is the most expensive thing to change later. It has to carry isolation (tenant, app),
causality (run, correlation, causation, hops), identity (principal, delegation), classification
and trace context, and it should be readable by existing tools.

## Decision

- Use **CloudEvents 1.0** context attributes (`specversion`, `id`, `source`, `type`, `subject`,
  `time`, `datacontenttype`, `dataschema`, `data`), with `type` holding the logical topic
  (ADR-003).
- Add **extension attributes** (lowercase alphanumeric, as CloudEvents requires): `tenantid`,
  `appid`, `runid`, `correlationid`, `causationid`, `hopcount`, `principal`, `onbehalfof`,
  `dataclass`, `traceparent` (the CloudEvents distributed-tracing extension) and
  `idempotencykey`.
- **Ids are ULIDs**: unique, time-sortable, 26 characters; generated monotonically per process.
- `time` is always present and always UTC.
- Events are **immutable**. Causal children are created with `Event.derive()`, which copies the
  context, sets `causationid`, increments `hopcount` and never lowers `dataclass`.
- **Strict in v0.1:** unknown attributes are rejected. External CloudEvents enter through the
  access layer, which maps them, rather than flowing in raw.
- The Pydantic model is the source of truth; `specs/schemas/envelope.v1.json` is generated from
  it and checked in CI.

Full attribute rules: [specs/event-envelope.md](../../specs/event-envelope.md).

## Alternatives considered

| Option | Why not |
|---|---|
| Custom envelope | Loses existing CloudEvents tooling, SDKs and broker bindings for no gain |
| UUIDv4 ids | Not time-sortable; worse index locality in PostgreSQL |
| UUIDv7 ids | Equally good technically; ULIDs are shorter in text and already in the requirements. Revisit only if a backend needs native UUIDs (they convert losslessly) |
| Lenient extensions (accept unknown) | Typos in security-relevant attributes would pass silently; strictness can be relaxed later, not the reverse |

## Consequences

- Every adapter must round-trip the full envelope unchanged (conformance test
  `test_preserves_the_envelope`).
- Agent code never sets `tenantid`/`appid`; the SDK stamps them from its scope (WP 1.2).
- Adding an extension is a compatible change; removing or renaming one is a new envelope
  version.

## Implementation

`src/aios/core/envelope.py`, `src/aios/core/ids.py`; tests in `tests/unit/core/`.

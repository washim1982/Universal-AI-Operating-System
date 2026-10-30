# ADR-003: Topic grammar, NATS wildcards and tenant prefixing

- **Status:** Accepted — 29 Oct 2026, project owner (WP 0.4); implemented in `aios.core.topics` and `aios.fabric.subjects`
- **Date:** 2026-10-28
- **Deciders:** Tech lead
- **Requirements:** FR-EVT-02, FR-EVT-03, FR-EVT-04, FR-EVT-16

## Context

The original vision's "semantic topics" (`agent.research.*`) need precise rules: what a topic
looks like, how wildcards match, how commands differ from facts, and how tenants are isolated
on a shared broker. The rules must map directly onto NATS JetStream (v1.0) and still be
enforceable on PostgreSQL and in memory.

## Decision

- **Logical topics** are 2–16 lowercase tokens: `<domain>.<entity…>.<verb>`. Commands end in
  `requested`; everything else is a fact. Thirteen domains are reserved for the platform.
- **Wildcards are NATS's:** `*` = exactly one token, `>` = one or more trailing tokens.
- **One grammar everywhere:** subscriptions, capability patterns and tool permission patterns
  all use it. Tool names have exactly three tokens, so "all Zendesk tools" is `zendesk.>`.
  Manifest validation rejects patterns that can never match a tool name, with a hint.
- **Physical subjects** are `<tenantid>.<appid>.<topic>[.<routing suffix>]`, built only by the
  fabric. The catalog declares routing suffixes; `task.requested` is suffixed with its
  capability, which makes capability routing a plain subject match.
- **Isolation by construction:** subscribers pass a `Scope(tenant, app)` and a logical pattern;
  the fabric prefixes the pattern. Tenant and app ids are single DNS-label tokens, so a scoped
  pattern cannot match another tenant. `Scope.platform()` is for platform components only.

Details and examples: [specs/topic-grammar.md](../../specs/topic-grammar.md).

## Alternatives considered

| Option | Why not |
|---|---|
| Glob semantics (`*` = anything) | Does not map to NATS subjects; `*` would mean different things on different backends |
| Different grammars for topics and tool permissions | The requirements' own examples (`zendesk.*`) showed the confusion this causes |
| Tenant in a header, not the subject | Isolation would depend on every consumer filtering correctly; with subject prefixes the broker enforces it |
| Topic per agent | Couples producers to agent names; capabilities let any provider serve the work |

## Consequences

- `task.requested` alone matches no task request (they all carry a suffix); subscribers use
  `task.requested.>` or a capability prefix. Documented in the spec.
- On NATS, per-tenant permissions can be expressed as subject ACLs (`acme.>`), giving a second,
  broker-level isolation layer (ADR-013).
- The requirements examples were corrected to `zendesk.>` and `search.>`.

## Implementation

`src/aios/core/topics.py` (grammar, matcher, regexes published in the JSON Schemas),
`src/aios/fabric/subjects.py`. Property-based tests compare the matcher with an independent
regex implementation.

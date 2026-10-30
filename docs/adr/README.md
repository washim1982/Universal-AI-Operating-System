# Architecture decision records

One file per decision, numbered in the order they were raised (implementation plan §11).
Copy [template.md](template.md) for a new one.

**Lifecycle:** *Proposed* → reviewed → *Accepted* or *Rejected*. A later change is a new ADR
that *supersedes* the old one; accepted ADRs are never rewritten. The Phase 0 ADRs were
accepted by the project owner on 29 Oct 2026 (WP 0.4), after the ADR-004 and ADR-007 spikes
([spikes/README.md](../../spikes/README.md)).

| ADR | Decision | Status | Decide by |
|---|---|---|---|
| [001](ADR-001-modular-monolith.md) | Modular monolith, package layout, layering rules | Accepted (implemented) | Phase 0 |
| [002](ADR-002-event-envelope.md) | Event envelope: CloudEvents 1.0 plus AIOS extensions | Accepted (implemented) | Phase 0 |
| [003](ADR-003-topic-grammar.md) | Topic grammar, wildcard semantics, tenant prefixing | Accepted (implemented) | Phase 0 |
| [004](ADR-004-postgres-event-store.md) | PostgreSQL event store: ordering, read horizon, partitions, outbox | Accepted (spike passed NFR-01) | Phase 0 |
| [005](ADR-005-delivery-semantics.md) | Delivery: at-least-once, idempotent consumers, effectively-once side effects | Accepted (implemented in memory) | Phase 0 |
| [006](ADR-006-workflow-engine.md) | Workflow engine: event-sourced graph, no deterministic code replay | Accepted | Phase 0 |
| [007](ADR-007-policy-engine.md) | Policy engine: **Cedar** | Accepted (implemented in `aios.security.cedar`) | Phase 0 |
| [008](ADR-008-model-adapters.md) | Model adapters: own interface over provider SDKs, LiteLLM optional | Accepted | Phase 0 |
| [009](ADR-009-memory-backend.md) | Memory: pgvector by default; embedding model chosen by eval | Accepted | Phase 0 |
| [010](ADR-010-token-streaming.md) | Token streams outside the fabric (SSE / WebSocket) | Accepted | Phase 0 |
| 011 | Identity: OIDC provider and agent identity format | Not drafted | End of Phase 1 |
| [012](ADR-012-license.md) | License: **Apache-2.0 core, commercial enterprise edition** | Accepted (implemented) | Phase 0 |
| 013 | NATS topology; PostgreSQL outbox → JetStream relay | Not drafted | During Phase 3 |
| 014 | Agent isolation levels and sandbox technology | Not drafted | During Phase 3 |
| 015 | App packaging format (OCI artifact) and registry | Not drafted | During Phase 2 |

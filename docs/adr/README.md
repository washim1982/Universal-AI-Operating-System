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
| [011](ADR-011-identity.md) | Identity: OIDC for people, API keys for services, named workloads for agents | Proposed (implemented; awaiting the owner's review) | End of Phase 1 |
| [012](ADR-012-license.md) | License: **Apache-2.0 core, commercial enterprise edition** | Accepted (implemented) | Phase 0 |
| 013 | NATS topology; PostgreSQL outbox → JetStream relay | Not drafted | During Phase 3 |
| 014 | Agent isolation levels and sandbox technology | Not drafted | During Phase 3 |
| [015](ADR-015-app-packaging.md) | An App version is one JSON bundle, kept as an OCI artifact in any registry | Proposed (implemented; awaiting the owner's review) | During Phase 2 |
| [016](ADR-016-approvals.md) | Approvals: a task stops, and runs again once a person answers | Proposed (implemented; awaiting the owner's review) | During Phase 2 |
| [017](ADR-017-taint.md) | Taint on the envelope; a person's own request doesn't set it | Proposed (implemented; awaiting the owner's review) | During Phase 2 |
| [018](ADR-018-budgets.md) | Budgets checked before a call, against a ledger paid with the event | Proposed (implemented; awaiting the owner's review) | Tech lead |
| [019](ADR-019-installation.md) | Installation is desired state in the database, with consent that holds until it is changed | Proposed (implemented; awaiting the owner's review) | Phase 3 |
| [020](ADR-020-schema-setup.md) | Schema setup runs once per change, under one lock per schema | Proposed (implemented; awaiting the owner's review) | Phase 3 |
| [021](ADR-021-replay-and-fork.md) | Replay, fork and retry are new runs that answer from their source's record | Proposed (implemented; awaiting the owner's review) | Phase 3 |

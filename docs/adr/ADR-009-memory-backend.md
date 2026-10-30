# ADR-009: pgvector as the default memory backend; embedding model chosen by eval

- **Status:** Accepted — 29 Oct 2026, project owner (WP 0.4)
- **Date:** 2026-10-28
- **Deciders:** AI engineer
- **Requirements:** FR-MEM-01–13, NFR-09, NFR-10

## Context

The Memory OS needs vector, full-text and metadata search with tenant isolation and fast,
complete erasure (GDPR `forget`). The MVP should run on a laptop with one database.

## Decision

- **pgvector** in the same PostgreSQL 17 instance is the default backend: HNSW indexes,
  hybrid ranking (full-text + vector + recency + importance), row-level security per tenant,
  and erasure in one transaction with the source items.
- Qdrant is the second adapter (Phase 4); both pass one memory conformance suite.
- **Embedding model is configuration, not code.** Every stored vector records the model id and
  dimension, so items can be re-embedded and mixed-model queries are rejected.
- The **default embedding model** is chosen in WP 2.1 by an eval on the reference-app corpora:
  one open-weights model that runs on CPU for self-hosted installs, plus provider embeddings
  for cloud installs. `restricted` data is embedded only by a private deployment
  (FR-SEC-11).

## Alternatives considered

| Option | Why not |
|---|---|
| Qdrant (or another vector DB) from day one | A second stateful system for the MVP; erasure and tenancy span two stores |
| Fixed embedding model | Locks quality and cost; prevents private-only routing for restricted data |

## Consequences

- Index size and recall must be benchmarked at the NFR-04 scale before v1.0.
- Re-embedding jobs are part of the ingestion pipeline from the start.

## Implementation

WP 2.1: `src/aios/memory/` with the pgvector adapter; the compose stack already uses the
`pgvector/pgvector:pg17` image.

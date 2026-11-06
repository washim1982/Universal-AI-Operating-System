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

`src/aios/memory/`: `store.py` (the port and the in-memory adapter), `postgres.py` (pgvector),
`rank.py` (hybrid ranking, context packs, chunking) and `service.py` (policy, embeddings,
ingestion). `src/aios/testing/memory.py` is the conformance suite both adapters pass, and Qdrant
will run it in Phase 4.

Decisions made while building it, within this ADR:

- **Ranking lives above the store,** so every backend ranks alike. Reciprocal rank fusion combines
  keyword and vector positions, because their scores aren't comparable. Recency and importance
  only reorder near neighbours: at higher weights a good old match sank below fresher, weaker ones.
- **Vector search has a similarity floor** (cosine 0.25), because it always has a nearest neighbour
  however unrelated. Offering one would spend the budget and taint the agent for nothing. The value
  is a first estimate for the eval below to calibrate.
- **Embeddings are optional.** Without a usable model, items are stored and found by keyword, and
  search says so. Vectors can be added later. `restricted` text is only embedded by a private
  deployment, so it may be keyword-only.
- **One HNSW index per vector size,** created when that size is first stored, because the model is
  configuration and a column can't index mixed sizes. Sizes above 2,000 are searched exactly.
- **Ephemeral items stay in the process,** as requirements §5.5 says. A second worker can't see them.
- **Events never quote content or queries.** The log is append-only, so anything in it couldn't be
  forgotten.

**Still open:** the default embedding model. This ADR calls for an eval on the reference-app
corpora, which needs real models to compare, and no real embedding model has been called yet. The
`embedding` task still routes to placeholder entries in the default registry, so a default install
searches by keyword until an operator sets `AIOS_PRIVATE_EMBEDDING_MODEL` or fills them in.

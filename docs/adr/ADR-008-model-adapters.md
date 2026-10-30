# ADR-008: Own model interface over official provider SDKs; LiteLLM optional underneath

- **Status:** Accepted — 29 Oct 2026, project owner (WP 0.4)
- **Date:** 2026-10-28
- **Deciders:** AI engineer, tech lead
- **Requirements:** FR-MOD-01–07, NFR-14; risk R6

## Context

The Model Kernel routes by declarative policy, enforces data-class rules and budgets,
attributes every token and dollar, falls back between providers, and records calls for
replay. Those concerns must stay under AIOS control. Provider APIs change often (tool formats,
structured output, caching, model ids).

## Decision

- AIOS defines one **`ModelProvider` port** (generate, stream, embed; structured output; tool
  calls; usage and cost in a normalized form) and a **model registry** as configuration
  (capabilities, context window, prices, deployment type `public` / `private`).
- First-party adapters use the **official SDKs** of the first providers (Anthropic, OpenAI,
  Google in WP 2.5) and an OpenAI-compatible adapter for self-hosted servers (vLLM and similar).
- **LiteLLM is allowed underneath** a single adapter for the long tail of providers, not as the
  core abstraction: AIOS still owns routing, policy, budgets, accounting and recording.
- Routing never hard-codes vendors in code; policies name candidates (requirements §7.10) and
  are revised from eval results.

## Alternatives considered

| Option | Why not |
|---|---|
| LiteLLM (or a gateway such as Portkey) as the core interface | Accounting, policy and replay semantics would follow a third party's model; harder to guarantee NFR-14 |
| Only OpenAI-compatible endpoints | Loses provider-specific features (prompt caching, extended outputs) and exact usage reporting |

## Consequences

- Adding a provider = one adapter + the model-provider conformance suite (NFR-11).
- Nightly contract tests against live providers catch API churn (implementation plan §8).

## Implementation

WP 1.4: `src/aios/models/` (registry, router, adapters, accounting, cassettes).

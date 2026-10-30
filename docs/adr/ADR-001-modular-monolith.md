# ADR-001: Modular monolith with enforced layers

- **Status:** Accepted — 29 Oct 2026, project owner (WP 0.4); implemented in the Sprint 1 skeleton
- **Date:** 2026-10-28
- **Deciders:** Tech lead, team
- **Requirements:** NFR-10, NFR-11, NFR-12; implementation plan §2.2, §4

## Context

AIOS has ten components. Splitting them into services on day one multiplies deployment,
versioning and debugging cost before we know where the real seams and scaling pressure are.
Leaving them in one package without rules lets them grow into each other, which makes the
later split (Kubernetes, Phase 4) expensive.

## Decision

1. **One Python package, `aios`, with a `src/` layout**, deployed as two processes (API and
   worker) from the same image. Services are split out only when measurements require it.
2. **Four layers**, enforced by import-linter in CI and pre-commit:

   | Layer | Packages | May import |
   |---|---|---|
   | Access | `cli`, `api`, `sdk`, `controlplane` | everything below; each other |
   | Runtime | `kernel`, `workflow`, `models`, `tools`, `memory`, `security`, `human`, `observability` | `fabric`, `core` — **never each other** |
   | Fabric | `fabric` | `core` |
   | Core | `core` | nothing inside `aios` |

3. Runtime components interact through **events** or through **ports defined in `core`** (for
   example `PolicyDecisionPoint`, `SecretResolver`), implemented by one component and injected
   into the others at composition time in the access layer.
4. `aios.testing` (test helpers and conformance suites) sits outside the layers and may import
   anything; nothing in the layers imports it.
5. Reference apps live in `apps/` and become uv workspace members when they get code (WP 1.8).

## Alternatives considered

| Option | Why not |
|---|---|
| Microservices from the start | Ten deployables before the first demo; distributed debugging with no tracing yet |
| One package without rules | Cross-component imports make the Phase 4 split and adapter swaps (NFR-11) hard |
| One package per component in a uv workspace | Same isolation as import-linter, but more packaging overhead while interfaces still change weekly |

## Consequences

- Cross-component calls must go through events or a `core` port, which is slightly more code
  up front but keeps every interaction recorded (principle P1) and swappable.
- Splitting a runtime component into its own service later means deploying it with a fabric
  adapter instead of the in-process one; no import graph surgery.

## Implementation

`[tool.importlinter]` in `pyproject.toml` (contract "Layered architecture (ADR-001)");
`uv run lint-imports` runs in CI and pre-commit. Component packages exist as stubs so the
contract is meaningful from the first commit.

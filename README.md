# AIOS — an operating system for AI agents

> Working name. A trademark check is pending (requirements Q7, risk R12).

Users deploy **AI Apps**; AIOS runs the agents, workflows, models, memory and tools inside them,
on an event-sourced runtime with policy, budgets, approvals and replay at every boundary.

| Document | What it is |
|---|---|
| [project-requirement.md](project-requirement.md) | The original vision |
| [project-requirement-v2.md](project-requirement-v2.md) | Requirements specification v2.0 (FR / NFR ids) |
| [implementation-plan.md](implementation-plan.md) | Phased plan, work packages, risks, ADR backlog |
| [docs/adr/](docs/adr/) | Architecture decision records (Phase 0 ADRs accepted) |
| [docs/editions.md](docs/editions.md) | AIOS Core (Apache-2.0) and AIOS Enterprise (commercial) |
| [specs/](specs/) | Event envelope, topic grammar, event catalog, manifest schemas |
| [docs/traceability.md](docs/traceability.md) | FR id → work package → tests |

## Status

**Phase 0.** In place so far:

- `aios.core`: the event envelope (CloudEvents 1.0 + AIOS extensions), ULIDs, the topic
  grammar and wildcard matcher, the standard event catalog, and the Agent / App / Workflow
  manifest models, with JSON Schemas generated into `specs/`.
- `aios.fabric`: the `EventBus` port, an in-memory adapter, and the conformance suite that
  every backend must pass.
- `aios.core.policy` and `aios.security.cedar`: the policy decision port and the Cedar engine
  that compiles Policy manifests (ADR-007).
- The local stack (PostgreSQL 17 + pgvector, OpenTelemetry Collector, Jaeger), CI, the
  accepted Phase 0 ADRs with their spikes (`spikes/`), and acceptance tests for the reference
  apps (marked expected-to-fail until their work packages land).

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                       # create .venv with runtime and dev dependencies
uv run pytest                 # unit + conformance tests
uv run ruff check . && uv run ruff format --check .
uv run pyright                # strict type checking
uv run lint-imports           # layering contract (ADR-001)
uv run aios specs check       # generated specs are up to date
docker compose -f deploy/compose/compose.yaml up -d   # local stack
```

Behind a TLS-intercepting proxy, add `--system-certs` to `uv` commands (or set
`UV_SYSTEM_CERTS=1`).

Install the git hooks once with `uv run pre-commit install`.

## Repository layout

```
src/aios/
  core/           envelope, ids, topics, errors, catalog, manifests (depends on nothing internal)
  fabric/         EventBus port + adapters (in-memory now; PostgreSQL in WP 1.1, NATS in WP 4.1)
  security/       Cedar policy engine (ADR-007); identity, secrets, audit in WP 2.2
  kernel/ workflow/ models/ tools/ memory/ human/ observability/   runtime (stubs)
  controlplane/ api/ sdk/ cli/                                               access layer
specs/            generated JSON Schemas + hand-written spec pages
apps/             reference AI Apps (user stories now; code from WP 1.8)
deploy/compose/   local stack
docs/adr/         architecture decision records
tests/            unit, conformance, e2e (acceptance), integration, chaos, security
benchmarks/       fabric and policy benchmarks
spikes/           throwaway experiments that settled ADRs (results in spikes/README.md)
scripts/          CI helpers (DCO sign-off check)
```

Layering is enforced by import-linter: `core` ← `fabric` ← runtime components ← access layer.
Runtime components never import each other; they talk through events or ports in `core`.

## License

AIOS Core is licensed under the [Apache License 2.0](LICENSE) (ADR-012); see [NOTICE](NOTICE).
The Enterprise edition is a separate, commercially licensed product
([docs/editions.md](docs/editions.md)). Contributions are welcome under the DCO
([CONTRIBUTING.md](CONTRIBUTING.md)).

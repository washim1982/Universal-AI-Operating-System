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

**M1 "First Light"** (Phase 1): the AI Researcher runs end to end on a durable, traced,
event-sourced runtime that survives a crash. See the M1 status in the
[implementation plan](implementation-plan.md) and [docs/traceability.md](docs/traceability.md)
for what is done and what is open.

- **Event fabric** (`aios.fabric`): the `EventBus` port with an in-memory and a PostgreSQL
  adapter (ADR-004); both pass one 37-case conformance suite. 1,000 events/s at p99 under 30 ms.
- **Agent Kernel** (`aios.kernel`): agents written with `from aios import agent, Context`,
  dispatch by capability, budgets, lifecycle events, the hop limit.
- **Workflow Engine** (`aios.workflow`): an event-sourced graph engine with fan-out, bounded
  loops, retries, timeouts, approvals and budgets; the Python DSL compiles to its IR.
- **Model Kernel** (`aios.models`): policy routing (§7.10), Anthropic / OpenAI /
  OpenAI-compatible adapters, fallback, cost accounting, record/replay cassettes.
- **Tool OS** (`aios.tools`): registry, policy-checked invocation pipeline, MCP client, search,
  page fetch and sandboxed files.
- **Observability, API, CLI**: OpenTelemetry traces and Prometheus metrics from the event log,
  a FastAPI server, and the `aios` command.
- **Policy** (`aios.security.cedar`): the Cedar engine for Policy manifests (ADR-007); it
  replaces the interim manifest allowlist in WP 2.2.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                       # create .venv with runtime and dev dependencies
uv run pytest                 # unit, conformance and acceptance tests
```

Run the AI Researcher locally. The recorded cassette answers model calls, so no API key is
needed; drop `--cassettes` and set `ANTHROPIC_API_KEY` to use a real model.

```bash
uv run aios dev --app apps/ai-researcher --cassettes tests/cassettes/ai-researcher
```

In a second terminal:

```bash
uv run aios run ai-researcher "What changed in the OpenTelemetry GenAI semantic conventions in 2026?"
```

```bash
uv run aios trace <run-id> --app ai-researcher
```

The production shape is an API process and worker processes sharing PostgreSQL. Start the
local stack (PostgreSQL, OpenTelemetry Collector, Jaeger on http://localhost:16686), then:

```bash
docker compose -f deploy/compose/compose.yaml up -d
```

```bash
uv run aios worker --app apps/ai-researcher --database-url postgresql://aios:XXXXXX@localhost:5432/aios --cassettes tests/cassettes/ai-researcher
```

```bash
uv run aios api --app apps/ai-researcher --database-url postgresql://aios:XXXXXX@localhost:5432/aios
```

Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` for the worker to send traces to
Jaeger, and `AIOS_TEST_DATABASE_URL` to run the PostgreSQL conformance and acceptance tests.
`aios init <name>` scaffolds a new App.

Checks that CI runs:

```bash
uv run ruff check . && uv run ruff format --check .
uv run pyright                # strict type checking
uv run lint-imports           # layering contract (ADR-001)
uv run aios specs check       # generated specs are up to date
```

Behind a TLS-intercepting proxy, add `--system-certs` to `uv` commands (or set
`UV_SYSTEM_CERTS=1`). Install the git hooks once with `uv run pre-commit install`.

## Repository layout

```
src/aios/
  core/           envelope, ids, topics, catalog, manifests, ports, cassettes (no internal deps)
  fabric/         EventBus port, in-memory and PostgreSQL adapters (NATS in WP 4.1)
  kernel/         Agent Kernel: definitions, Context, worker
  workflow/       Workflow Engine: DSL, expressions, run-state fold, engine
  models/         Model Kernel: registry, router, provider adapters
  tools/          Tool OS: registry, pipeline, native/MCP/built-in providers
  observability/  traces, metrics, timeline and logs from the event log
  security/       Cedar policy engine (ADR-007); identity, secrets, audit in WP 2.2
  memory/ human/  Phase 2 (stubs)
  controlplane/   the composition root (Runtime) and App installation
  api/ cli/ sdk/  access layer
  testing/        conformance suites, LocalRuntime, fake MCP server
specs/            generated JSON Schemas + hand-written spec pages
apps/             reference AI Apps (AI Researcher has code; Support Desk in WP 2.7)
deploy/compose/   local stack
docs/adr/         architecture decision records
tests/            unit, conformance, e2e (acceptance), cassettes
benchmarks/       fabric and policy benchmarks
spikes/           throwaway experiments that settled ADRs (results in spikes/README.md)
scripts/          CI helpers (DCO sign-off check)
```

Layering is enforced by import-linter: `core` ← `fabric` ← runtime components ← access layer.
Runtime components never import each other; they talk through events or ports in `core`, and
`aios.controlplane` wires them together.

## License

AIOS Core is licensed under the [Apache License 2.0](LICENSE) (ADR-012); see [NOTICE](NOTICE).
The Enterprise edition is a separate, commercially licensed product
([docs/editions.md](docs/editions.md)). Contributions are welcome under the DCO
([CONTRIBUTING.md](CONTRIBUTING.md)).

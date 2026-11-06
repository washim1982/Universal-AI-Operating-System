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
| [docs/user-guide.md](docs/user-guide.md) | How to use AIOS, for each role: administrators, operators, members, approvers, auditors and App developers |
| [docs/testing-guide.md](docs/testing-guide.md) | Step-by-step setup and test process for the reference apps |

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
  loops, retries, timeouts, approvals, budgets, signals, and pause and resume; the Python DSL
  compiles to its IR.
- **Model Kernel** (`aios.models`): policy routing (§7.10), Anthropic / OpenAI / Google Gemini /
  OpenAI-compatible adapters, embeddings, fallback, cost accounting, record/replay cassettes. Hard
  and soft budgets per run, user, App and tenant, checked before each call, with a downgrade at the
  soft limit, and provider prompt caching that is counted and priced (WP 2.5, ADR-018).
- **Tool OS** (`aios.tools`): registry, policy-checked invocation pipeline, service credentials
  injected at execution time, MCP client, search, page fetch and sandboxed files.
- **Observability, API, CLI**: OpenTelemetry traces and Prometheus metrics from the event log,
  a FastAPI server, and the `aios` command.
- **Security Kernel** (`aios.security`): sign-in with API keys and OIDC, Cedar policies at every
  enforcement point built so far, row-level security in PostgreSQL, a hash-chained audit log, a
  kill switch, and secrets from an encrypted file or Vault (WP 2.2).
- **Memory OS** (`aios.memory`): seven scopes bound to the caller, hybrid keyword and vector search
  on pgvector, cited context packs within a token budget, document ingestion with re-sync, and
  erasure. Retrieved content is untrusted and keeps its data label (WP 2.1).
- **AI threat controls** (WP 2.3): untrusted content taints the whole run, so a poisoned document
  read in one step can't drive a refund in the next. Egress allowlists per App and tool, a
  pluggable injection classifier, and `restricted` data kept to approved models. A red-team
  corpus runs in CI, assuming the model is fooled every time. See
  [docs/threat-model.md](docs/threat-model.md).
- **Human OS** (`aios.human`): a tool call or workflow step can wait for a person. The run uses no
  compute meanwhile, survives restarts, and the action happens once or not at all. Silence means no,
  and whoever an action is for can't approve it. REST, a signed webhook and `aios approvals`
  (WP 2.4, ADR-016).
- **Console** (`console/`): a web app with no permissions of its own: it calls the API as the
  person using it. Approvals (WP 2.6); and a Trace Explorer that draws a run's steps with their
  model calls, tool calls, decisions and costs, Apps and consent, agents and the kill switch, costs
  and budgets, a policy simulator, a memory browser, and the event stream (WP 3.2).
- **Installing Apps** (`aios app`, WP 3.1): an App version is a reproducible bundle, pushed to
  any OCI registry (ADR-015). Installing it waits for a tenant admin, and what they refuse is
  blocked at runtime and stays refused across upgrades. Configuration, upgrade, rollback, and
  uninstall with retain, export or delete; every process reconciles to what is installed;
  schedules and signed webhooks start runs (ADR-019).
- **Replay and fork** (WP 3.3): run a finished run again with every model and tool call answered
  from its record, at no cost. Or fork it from a step with a new input, step input or model, with
  side effects held back. Or retry it from the step that failed, for real. Compare any two runs
  step by step (`aios replay`, `fork`, `retry`, `compare`, and in the console; ADR-021).

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                       # create .venv with runtime and dev dependencies
uv run pytest                 # unit, conformance and acceptance tests
```

Run the AI Researcher locally. The recorded cassette answers model calls, so no API key is
needed, but it returns the same report whatever you ask.

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

Or open http://127.0.0.1:8080/demo while `aios dev` runs: ask a question, watch each step, then
read the report with its sources and the reviewer's verdict. The page says when it is showing
the recording. It has no sign-in, so `aios api` does not serve it.

For real answers, start the server without `--cassettes`, with an Anthropic API key and a
[Brave Search API](https://brave.com/search/api/) key in its environment:

```bash
export ANTHROPIC_API_KEY=...
export AIOS_SEARCH_API_KEY=...
uv run aios dev --app apps/ai-researcher
```

A live run takes a few minutes, because the research tasks of one run execute one at a time
(per-run ordering, ADR-005), and it costs real money: the workflow stops a run once it passes
$3.00. If a key is wrong, `aios run` ends with `status=failed` and the provider's error.

To stay on your own machine, point AIOS at any OpenAI-compatible server (llama.cpp, vLLM,
Ollama). Every call in the default model policy then goes to that one model, and costs nothing.
[Keenable](https://keenable.ai) is an alternative to Brave for search:

```bash
export AIOS_PRIVATE_MODEL=<model-id>                    # as listed by the server's /v1/models
export AIOS_PRIVATE_MODEL_URL=http://127.0.0.1:8181/v1
export AIOS_PRIVATE_MODEL_KEY=...                       # if the server needs one
export AIOS_SEARCH_PROVIDER=keenable
export AIOS_SEARCH_API_KEY=...
uv run aios dev --app apps/ai-researcher
```

The same model answers embedding calls unless `AIOS_PRIVATE_EMBEDDING_MODEL` names another. A
tool's service credentials only reach a tool whose App lists them under `spec.config.secrets`.
They come from Vault if `VAULT_ADDR` and `VAULT_TOKEN` are set, from an encrypted file if
`AIOS_SECRETS_FILE` and `AIOS_SECRETS_KEY` are set (`aios secrets set` fills it), and otherwise
from `AIOS_SECRET_<NAME>`.

The production shape is an API process and worker processes sharing PostgreSQL. Start the
local stack (PostgreSQL, OpenTelemetry Collector, Jaeger on http://localhost:16686), then:

```bash
docker compose -f deploy/compose/compose.yaml up -d
```

```bash
uv run aios worker --app apps/ai-researcher --database-url postgresql://aios:XXXXXX@127.0.0.1:5432/aios --cassettes tests/cassettes/ai-researcher
```

```bash
uv run aios api --app apps/ai-researcher --database-url postgresql://aios:XXXXXX@127.0.0.1:5432/aios
```

`aios api` requires a credential on every request, and the tenant comes from that credential.
Create a tenant's first key with direct database access, and put it in `AIOS_API_KEY`:

```bash
export AIOS_API_KEY=$(uv run aios platform --database-url postgresql://aios:XXXXXX@127.0.0.1:5432/aios key --tenant local --name admin)
```

Setting `AIOS_OIDC_ISSUER` and `AIOS_OIDC_AUDIENCE` also turns on sign-in with an identity provider;
`docker compose --profile security` starts Keycloak and OpenBao for that. `--no-auth` skips sign-in
on a loopback address, and repeating `--tenant` installs the Apps for several tenants. See
[ADR-011](docs/adr/ADR-011-identity.md) for identities and roles.

Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` for the worker to send traces to
Jaeger, and `AIOS_TEST_DATABASE_URL` to run the PostgreSQL conformance, isolation and acceptance
tests. `aios init <name>` scaffolds a new App. `aios status`, `pause`, `resume`, `cancel` and
`signal` control a running run; a pause takes effect at the next step or fan-out item. `aios replay`
runs a finished run again from its record, `aios fork --from <step>` starts it again from a step
with side effects held back, `aios retry` does the failed work again for real, and `aios compare`
shows two runs side by side. `aios audit
verify` checks the tenant's audit chain, `aios keys` manages API keys, `aios suspend` stops an
agent, an App or a tenant, and `aios memory` ingests, searches and erases memory.

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
  security/       identity, Cedar policies, enforcement, audit chain, secrets
  memory/         Memory OS: scopes, ranking, ingestion, pgvector adapter
  human/          Human OS: approvals, the inbox and the webhook
  human/          Phase 2 (stub)
  controlplane/   the composition root (Runtime) and App installation
  api/ cli/ sdk/  access layer
  testing/        conformance suites, LocalRuntime, fake MCP server
specs/            generated JSON Schemas + hand-written spec pages
apps/             reference AI Apps: the AI Researcher and the AI Support Desk
console/          the web console (Next.js)
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

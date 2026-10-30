# Requirements traceability

FR id → work package → status → tests. Seeded in Sprint 1 from
[project-requirement-v2.md](../project-requirement-v2.md) and
[implementation-plan.md](../implementation-plan.md); updated at every sprint review
(implementation plan §14). `tests/unit/test_docs.py` fails if an FR id is missing here.

Status: **Not started** · **Started** (partly built; see notes) · **Done** (exit criteria
met, tests green).

| FR | Priority | Requirement | Work packages | Status | Notes / tests |
|---|---|---|---|---|---|
| FR-APP-01 | P0 | An App manifest (§7.4) declares agents, workflows, tools, memory configuration, policies, model policy, … | 1.7 | Started | Manifest schema v1alpha1; `tests/unit/core/test_manifests.py, tests/unit/test_docs.py` |
| FR-APP-02 | P0/P1 | Entry points: chat (REST/WebSocket) and event triggers (P0); schedules, webhooks, OpenAI-compatible API … | 1.7, 3.1, 4.5 | Not started |  |
| FR-APP-03 | P1 | Installing an App into a tenant lists the permissions it requests (tools, memory scopes, data classes, … | 3.1 | Not started |  |
| FR-APP-04 | P1 | Versioned upgrade and rollback; in-flight runs finish on the version they started with. | 3.1 | Not started |  |
| FR-APP-05 | P1 | Uninstall with an explicit data choice: retain, export or delete. | 3.1 | Not started |  |
| FR-APP-06 | P1/P2 | Apps are packaged as OCI artifacts and published to a private registry (P1); public marketplace (P2). | 3.1, Phase 5 | Not started |  |
| FR-APP-07 | P0 | Per-install configuration and secret bindings are validated against the App's config schema before activation. | 1.7 | Not started |  |
| FR-APP-08 | P2 | Environments per install (dev, staging, prod) with promotion between them. | Phase 5 | Not started |  |
| FR-EVT-01 | P0 | All communication between runtime components uses events in the AIOS envelope (CloudEvents 1.0 plus AIOS … | 1.1 | Started | Envelope model + JSON Schema; `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-02 | P0 | Commands (`*.requested`, consumed by exactly one handler) are distinct from facts (past tense, broadcast … | 1.1 | Started | Command/fact classification; `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-03 | P0 | Hierarchical topic grammar with NATS-compatible wildcards: `*` matches one token, `>` matches all … | 1.1 | Started | Grammar + matcher; `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-04 | P0 | The fabric applies tenant and app namespacing. Agent code never builds physical subjects and can't … | 1.1 | Started | Scoped subjects (in-memory); `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-05 | P0 | At-least-once delivery; competing consumer groups and broadcast subscriptions; ordering guaranteed per … | 1.1 | Started | Groups, per-run order (in-memory); `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-06 | P0 | Every event has a unique id; consumers deduplicate through an inbox keyed by event id or idempotency key. … | 1.1 | Started | Unique ULID ids; inbox pending; `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-07 | P0 | Transactional publish: on the PostgreSQL backend, state changes and their events commit atomically (outbox … | 1.1 | Not started | Design validated by the ADR-004 spike (`spikes/pg_sequencer.py`) |
| FR-EVT-08 | P0 | Retries with exponential backoff and jitter up to a maximum number of attempts, then a dead-letter queue … | 1.1 | Started | Retries, DLQ, redrive (in-memory); `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-09 | P0 | Replay by position, time range or run, into a new consumer group or a sandbox with side effects disabled. | 1.1 | Started | Replay by position/time/run (in-memory); `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-10 | P0/P1 | Schemas: a JSON Schema per event type and version, validation on publish and backward-compatibility checks … | 1.1, 3.6 | Started | Catalog schemas, dev-mode validation; `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-11 | P0 | Loop and storm protection: each causally derived event increments `hopcount`; publishing beyond the limit … | 1.1 | Started | Hop limit on publish; `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-12 | P0 | A request/reply helper over events (correlation id plus timeout) for call-style APIs. | 1.1 | Started | request() helper; `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-13 | P1 | Payloads above 256 KB go to object storage and the event carries a reference (claim-check pattern). | 4.1 | Not started |  |
| FR-EVT-14 | P1 | Retention policy per event domain, archival to object storage and legal hold. | 4.1 | Not started |  |
| FR-EVT-15 | P0–P2 | Backends behind one `EventBus` port: in-memory and PostgreSQL (P0), NATS JetStream (P1), Kafka/Redpanda … | 1.1, 4.1, Phase 5 | Started | Port + in-memory adapter + conformance suite; `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-16 | P0/P2 | Capability-based routing: a `task.requested` event carries a capability and is dispatched to a healthy … | 1.1, Phase 5 | Started | Capability routing suffix; `tests/conformance/test_memory_bus.py, tests/unit/core/test_topics.py` |
| FR-EVT-17 | P0 | Model token streams travel over a separate streaming channel (SSE/WebSocket), not the fabric; only summary … | 1.1 | Not started |  |
| FR-AGT-01 | P0 | An Agent manifest (§7.3) declares identity, version, capabilities with typed input and output schemas, … | 1.2 | Started | Manifest schema v1alpha1; `tests/unit/core/test_manifests.py, tests/unit/test_docs.py` |
| FR-AGT-02 | P0/P1 | Runtime types: native Python via the SDK (P0); container image, remote A2A agent, and framework adapters … | 1.2, 3.6, 4.2 | Not started |  |
| FR-AGT-03 | P0 | A lifecycle state machine — `Registered → Starting → Ready ⇄ Busy → Suspended → Draining → Stopped`, plus … | 1.2 | Not started |  |
| FR-AGT-04 | P0 | Supervision with restart policies (`Always`, `OnFailure`, `Never`) and backoff; a crashing agent can't … | 1.2 | Not started |  |
| FR-AGT-05 | P0/P1 | Dispatch by capability, health and load, with per-agent concurrency limits (P0); placement by labels such … | 1.2, 4.2 | Not started |  |
| FR-AGT-06 | P1 | Scaling: minimum and maximum instances, scale to zero, and autoscaling on queue depth or latency. | 4.2 | Not started |  |
| FR-AGT-07 | P0/P2 | Agents are stateless by default, with state held in the run and in Memory OS (P0). Optional stateful … | 1.2, Phase 5 | Not started |  |
| FR-AGT-08 | P0 | Per-task limits on steps, tokens, cost and wall-clock time; exceeding one fails the task with … | 1.2 | Not started |  |
| FR-AGT-09 | P0/P1 | Isolation levels: in-process for trusted first-party agents (P0); subprocess, container, and gVisor or … | 1.2, 4.4 | Not started |  |
| FR-AGT-10 | P0 | Each agent has its own workload identity for policy and audit. No shared superuser identity. | 1.2 | Not started |  |
| FR-AGT-11 | P1 | Several versions of an agent run side by side, with canary traffic splitting and rollback. | 4.2 | Not started |  |
| FR-AGT-12 | P0 | Liveness and readiness checks plus heartbeats; unhealthy instances are removed from dispatch. | 1.2 | Not started |  |
| FR-AGT-13 | P0 | Hot reload of agent code and manifests in dev mode. | 1.2 | Not started |  |
| FR-WF-01 | P0–P2 | Workflows are written in a Python DSL (P0) or YAML/JSON (P1). Both compile to one versioned graph IR, … | 1.3, 3.6, Phase 5 | Started | Graph IR schema v0.1; `tests/unit/core/test_manifests.py` |
| FR-WF-02 | P0/P1 | Node types: agent task (by capability), tool call, model call, human approval or task, condition, parallel … | 1.3, 4.8 | Not started |  |
| FR-WF-03 | P0 | Dynamic planning: a planner agent may emit tasks or a sub-graph at runtime. The engine validates it … | 1.3 | Not started |  |
| FR-WF-04 | P0 | Durable execution: every state transition is an event, run state is a fold over those events, and after a … | 1.3 | Not started |  |
| FR-WF-05 | P0 | Per-step retry policy, timeout and error routing (`on_error` edges). | 1.3 | Not started |  |
| FR-WF-06 | P1 | Saga-style compensation for steps with external side effects. | 4.8 | Not started |  |
| FR-WF-07 | P0 | Waits of hours to weeks consume no compute; timers are durable. | 1.3 | Not started |  |
| FR-WF-08 | P0 | Signals (external input into a running run) and queries (read its current state). | 1.3 | Not started |  |
| FR-WF-09 | P0 | Runs pin the definition version they started with; new runs use the latest version. | 1.3 | Not started |  |
| FR-WF-10 | P0 | Run-level budgets (tokens, cost, steps, wall-clock, hops) enforced across all steps. | 1.3 | Not started |  |
| FR-WF-11 | P0/P1 | Run control: start, cancel, pause and resume (P0); retry from a step and fork from a step (P1). | 1.3, 3.3 | Not started |  |
| FR-WF-12 | P0 | Typed step outputs passed by reference expressions; large artifacts passed by reference to object storage. | 1.3 | Not started |  |
| FR-WF-13 | P1 | Concurrency limits per workflow and per tenant; queued runs with priorities. | 4.2 | Not started |  |
| FR-WF-14 | P2 | Optional Temporal backend adapter for organizations standardized on Temporal. | Phase 5 | Not started |  |
| FR-MEM-01 | P0 | The seven scopes and four retention classes above. | 2.1 | Not started |  |
| FR-MEM-02 | P0 | `put`, `search`, `recall`, `forget`, `get` and `update` with identical semantics on every backend (§7.7). | 2.1 | Not started |  |
| FR-MEM-03 | P0 | `recall` builds a context pack for a task within a token budget: multi-scope hybrid retrieval (vector, … | 2.1 | Not started |  |
| FR-MEM-04 | P0 | Every item carries provenance (source event id, author principal, time, source URI) and a sensitivity label. | 2.1 | Not started |  |
| FR-MEM-05 | P0 | The Security Kernel checks every read and write. Write permission is granted per scope, which limits … | 2.1 | Not started |  |
| FR-MEM-06 | P0 | Tenant isolation in storage (a namespace per tenant plus row-level security); similarity search never … | 2.1 | Not started |  |
| FR-MEM-07 | P0/P1 | Document ingestion with chunking, embedding, incremental re-sync and deletion propagation, for files and … | 2.1, 4.7 | Not started |  |
| FR-MEM-08 | P1 | An embedding model registry; each item records its embedding model version, and a background job … | 4.7 | Not started |  |
| FR-MEM-09 | P1 | Consolidation jobs summarize sessions into user and agent memory, extract facts and expire low-value … | 4.7 | Not started |  |
| FR-MEM-10 | P0/P1 | Erasure: `forget(subject=…)` deletes items and embeddings (P0) plus caches and derived summaries (P1), and … | 2.1 | Not started |  |
| FR-MEM-11 | P0–P2 | Backends through adapters: pgvector (P0); Qdrant (P1); Weaviate, Pinecone, Azure AI Search and graph … | 2.1, Phase 5 | Not started |  |
| FR-MEM-12 | P1 | Retrieval quality evaluation (recall@k, MRR, groundedness) on golden datasets. | 3.4 | Not started |  |
| FR-MEM-13 | P0 | Retrieved content is tagged untrusted for injection defenses (FR-SEC-09). | 2.1 | Not started |  |
| FR-MOD-01 | P0/P1 | One interface for generation, tool calling, structured output (Pydantic / JSON Schema), embeddings and … | 1.4, 4.6 | Not started |  |
| FR-MOD-02 | P0/P1 | Provider adapters: Anthropic, OpenAI, Google Gemini, and any OpenAI-compatible endpoint, which covers … | 1.4, 2.5, 4.6 | Not started |  |
| FR-MOD-03 | P0 | A model registry held as configuration: capabilities, context window, prices, latency profile, deployment … | 1.4 | Not started |  |
| FR-MOD-04 | P0 | Declarative routing policies (§7.10) match on task class, data classification, tenant tier, budget, … | 1.4 | Not started |  |
| FR-MOD-05 | P0/P1 | Reliability: retries with backoff, provider fallback chains and timeouts (P0); circuit breakers, … | 1.4, 4.6 | Not started |  |
| FR-MOD-06 | P0 | Data-classification guard: `restricted` content is only ever routed to approved deployments (private, or … | 2.3 | Not started |  |
| FR-MOD-07 | P0 | Token and cost accounting for every call, attributed to tenant, App, agent, run and user, and published as … | 1.4 | Not started |  |
| FR-MOD-08 | P0 | Hard and soft budgets per run, App, tenant and user. A soft limit raises an alert and downgrades the … | 2.5 | Not started |  |
| FR-MOD-09 | P0/P1 | Caching: provider prompt caching (P0); an exact-match response cache and an opt-in, tenant-isolated … | 2.5, 4.6 | Not started |  |
| FR-MOD-10 | P0/P1 | Guardrail hooks before and after each call (P0); built-in PII redaction, injection detection, output … | 1.4, 4.6 | Not started |  |
| FR-MOD-11 | P2 | Adaptive routing driven by eval scores, cost and latency telemetry, e.g., try a small model first and … | Phase 5 | Not started |  |
| FR-MOD-12 | P0 | Record/replay mode for deterministic tests and run replay. | 1.4 | Not started |  |
| FR-TOOL-01 | P0 | A tool registry with qualified names `<provider>.<resource>.<action>` (e.g., `github.pr.create`), JSON … | 1.5 | Not started |  |
| FR-TOOL-02 | P0 | Side-effect classes `read`, `write`, `destructive`, `external_comm`, `financial` and `code_exec`; policies … | 1.5 | Not started | Side-effect classes defined in `aios.core.policy.SideEffect` |
| FR-TOOL-03 | P0/P1 | Logical-to-physical resolution: `tool.invoke("filesystem.file.read")` resolves to a provider binding (P0), … | 1.5, 3.1 | Not started |  |
| FR-TOOL-04 | P0–P2 | Provider types: native Python, MCP servers (stdio and streamable HTTP, importing tools, resources and … | 1.5, 4.7, Phase 5 | Not started |  |
| FR-TOOL-05 | P0 | Invocation pipeline: validate input → check policy → request approval if required → inject credentials → … | 1.5 | Not started |  |
| FR-TOOL-06 | P0/P1 | Credentials: service credentials (P0) and user-delegated OAuth on-behalf-of (P1). Secrets never enter … | 1.5, 4.7 | Not started |  |
| FR-TOOL-07 | P0/P1 | Idempotency keys for write tools (P0); dry-run previews where the provider supports them, shown in … | 1.5, 3.5 | Not started |  |
| FR-TOOL-08 | P0/P1 | Per-tool timeouts (P0); rate limits, concurrency limits and cost attribution (P1). | 1.5, 4.7 | Not started |  |
| FR-TOOL-09 | P0/P1 | MCP supply-chain safety: a server allowlist and pinning tool descriptions by hash, alerting or blocking … | 1.5, 4.7 | Not started |  |
| FR-SEC-01 | P0 | Tenancy hierarchy: Organization → Tenant → App install → Environment. The tenant id is on every record, … | 2.2 | Not started |  |
| FR-SEC-02 | P0–P2 | Identities for all principals: users via OIDC, service accounts via API keys, and agents via workload … | 2.2, 4.8, Phase 5 | Not started |  |
| FR-SEC-03 | P0 | Authorization through an embeddable policy engine (Cedar, decided in ADR-007) that supports RBAC … | 2.2 | Started | Cedar engine, Policy manifest → Cedar compiler with schema validation, default deny (ADR-007); `tests/unit/security/test_cedar.py, tests/unit/core/test_policy_manifests.py` |
| FR-SEC-04 | P0 | Policy enforcement points on publish/subscribe, task dispatch, tool invocation, memory read/write, model … | 2.2 | Started | `PolicyDecisionPoint` port in `aios.core.policy` with an `AllowAll` default; enforcement points land with their components; `tests/unit/core/test_policy_manifests.py` |
| FR-SEC-05 | P0 | Delegation: an agent acting for a user gets the intersection of its own permissions and the user's. The … | 2.2 | Started | Delegation = agent ∩ user in the Cedar engine; `tests/unit/security/test_cedar.py` |
| FR-SEC-06 | P0 | Storage isolation: PostgreSQL row-level security by tenant, per-tenant memory namespaces and per-tenant … | 2.2 | Not started |  |
| FR-SEC-07 | P0/P1 | Secrets through a pluggable vault: a local encrypted store and HashiCorp Vault/OpenBao (P0); AWS, Azure … | 2.2, 4.8 | Not started |  |
| FR-SEC-08 | P0/P1 | An append-only, hash-chained audit log of policy decisions, tool calls, approvals, admin changes and data … | 2.2, 4.8 | Not started |  |
| FR-SEC-09 | P0 | Prompt-injection defense in depth: provenance and taint tracking of untrusted content. While a run's … | 2.3 | Started | Policies escalate side-effect classes while the context is tainted (Cedar); taint tracking itself not started; `tests/unit/security/test_cedar.py` |
| FR-SEC-10 | P0 | Egress control: allowlisted destinations per App and tool; requests that carry data to hosts not on the … | 2.3 | Not started |  |
| FR-SEC-11 | P0/P1 | Data classification labels (`public`, `internal`, `confidential`, `restricted`) propagate from sources to … | 2.2, 4.8 | Started | Labels on the envelope; propagate on derive; `tests/unit/core/test_envelope.py` |
| FR-SEC-12 | P0/P2 | TLS in transit and encryption at rest (P0); per-tenant keys and BYOK (P2). | 2.2, Phase 5 | Not started |  |
| FR-SEC-13 | P0 | Kill switch: suspend an agent, App or tenant immediately and revoke its credentials. | 2.2 | Not started |  |
| FR-HUM-01 | P0 | Approval exists both as an explicit workflow node and as a policy outcome (`requireApproval`) on any … | 2.4 | Started | `requireApproval` policy outcome (Cedar) and the approval node in the workflow IR; `tests/unit/security/test_cedar.py, tests/unit/core/test_manifests.py` |
| FR-HUM-02 | P0 | Events: `approval.requested`, `approval.granted`, `approval.denied`, `approval.modified`, … | 2.4 | Not started |  |
| FR-HUM-03 | P0 | Each request shows the action summary, the exact payload or diff, the risk level, the policy that … | 2.4 | Not started |  |
| FR-HUM-04 | P0/P1 | Routing to users or roles, with separation of duties: whoever initiated an action can't approve it (P0). … | 2.4, 3.5 | Not started |  |
| FR-HUM-05 | P0/P1 | Timeouts with explicit behavior: deny by default, auto-approve only where policy allows (P0); reminders … | 2.4, 3.5 | Not started |  |
| FR-HUM-06 | P1 | Approve with edits: the approver changes parameters, and the modified action is re-checked against policy. | 3.5 | Not started |  |
| FR-HUM-07 | P0–P2 | Channels: console inbox and REST/webhook (P0); Slack, Microsoft Teams and email with signed action links … | 2.4, 3.5, Phase 5 | Not started |  |
| FR-HUM-08 | P1 | Humans as capability providers: users or groups registered for a capability (e.g., `legal.review`) receive … | 3.5 | Not started |  |
| FR-HUM-09 | P1 | Feedback capture (ratings, corrections) linked to the run and step, feeding evals and memory. | 3.5 | Not started |  |
| FR-HUM-10 | P0 | Paused runs consume no compute and survive restarts and deployments. | 2.4 | Not started |  |
| FR-OBS-01 | P0 | Every event is persisted and can be queried by run, tenant, App, agent, type and time. | 1.6 | Not started |  |
| FR-OBS-02 | P0 | OpenTelemetry tracing across all components. W3C trace context travels in the event envelope. Model, tool, … | 1.6 | Not started |  |
| FR-OBS-03 | P0/P1 | Trace Explorer: a run shown as a DAG and timeline, with per-node inputs and outputs (redacted per policy), … | 1.6, 3.2 | Not started |  |
| FR-OBS-04 | P1 | Replay and fork: re-render a past run exactly from its recorded events, or fork from any step with a … | 3.3 | Not started |  |
| FR-OBS-05 | P0 | Metrics: run outcomes, latency (including time to first token), tokens, cost, cache hit rate, approval … | 1.6 | Not started |  |
| FR-OBS-06 | P1 | Cost explorer by tenant, App, agent, model and user, with budget alerts. | 3.2 | Not started |  |
| FR-OBS-07 | P1 | Evaluation framework: datasets; deterministic, schema, LLM-as-judge and human graders; experiment … | 3.4 | Not started |  |
| FR-OBS-08 | P0 | Export to external tools (Jaeger, Tempo, Datadog, Langfuse, Phoenix and others) over OTLP. | 1.6 | Not started |  |
| FR-OBS-09 | P0 | Structured JSON logs correlated by run and trace ids, with PII and secret redaction. | 1.6 | Not started |  |
| FR-OBS-10 | P1 | Alerts on SLO breaches, budget thresholds, error spikes and dead-letter growth. | 4.8 | Not started |  |
| FR-CP-01 | P0/P1 | Declarative resources (`App`, `Agent`, `Workflow`, `Tool`, `ToolProvider`, `ModelProvider`, `ModelPolicy`, … | 1.7, 3.1 | Not started |  |
| FR-CP-02 | P0 | A REST API described by OpenAPI 3.1, plus SSE/WebSocket streaming; the event catalog published as an … | 1.7 | Not started |  |
| FR-CP-03 | P0/P1 | `aios` CLI: `init`, `dev`, `apply`, `get`, `run`, `logs`, `trace`, `events tail`, `dlq` (P0); `describe`, … | 1.7, 3.6 | Not started |  |
| FR-CP-04 | P0/P1 | Python SDK for writing agents and as an API client (P0); TypeScript client SDK (P1). | 1.7, 3.6 | Not started |  |
| FR-CP-05 | P0 | Local dev mode: one command starts the full OS (API, worker, PostgreSQL with pgvector, OTel Collector, … | 1.7 | Not started |  |
| FR-CP-06 | P1 | Templates and scaffolding for Apps, agents and tools. | 3.6 | Not started |  |
| FR-UI-01 | P1 | Apps: catalog, installed apps, configuration and permission consent. | 3.2 | Not started |  |
| FR-UI-02 | P1 | Agents: status, lifecycle controls, versions and metrics. | 3.2 | Not started |  |
| FR-UI-03 | P1 | Runs: search, live view, Trace Explorer, replay and fork. | 3.2 | Not started |  |
| FR-UI-04 | P0/P1 | Approvals inbox with full context and approve / deny / modify actions: minimal (P0), complete (P1). | 2.6, 3.2 | Not started |  |
| FR-UI-05 | P1 | Cost and usage dashboards with budgets. | 3.2 | Not started |  |
| FR-UI-06 | P1 | Policy viewer and simulator ("would this action be allowed?"). | 3.2 | Not started |  |
| FR-UI-07 | P1 | Memory browser to inspect, edit and delete items, with audit. | 3.2 | Not started |  |
| FR-UI-08 | P2 | Visual workflow designer that edits the same graph IR. | Phase 5 | Not started |  |
| FR-UI-09 | P1 | Event stream explorer and dead-letter queue management. | 3.2 | Not started |  |
| FR-INT-01 | P0/P1 | MCP client for tools, resources and prompts (P0); MCP server that exposes Apps and agents to external MCP … | 1.5, 3.7 | Not started |  |
| FR-INT-02 | P1 | A2A: publish Agent Cards for AIOS agents, use remote A2A agents as capability providers, and verify signed … | 4.5 | Not started |  |
| FR-INT-03 | P1 | An OpenAI-compatible chat endpoint per App, so existing clients and SDKs can talk to AI Apps. | 3.7 | Not started |  |
| FR-INT-04 | P1/P2 | Framework adapters that host agents built with LangGraph and OpenAI Agents SDK (P1), and with CrewAI, … | 3.6, Phase 5 | Not started |  |
| FR-INT-05 | P1 | Inbound and outbound webhooks; CloudEvents over HTTP for external event sources. | 4.5 | Not started |  |

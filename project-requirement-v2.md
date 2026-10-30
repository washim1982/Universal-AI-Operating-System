# Universal AI Operating System (AIOS)

## Requirements Specification — v2.0

| | |
|---|---|
| **Status** | Draft for review |
| **Date** | 28 October 2026 |
| **Based on** | [project-requirement.md](project-requirement.md) (original vision, kept unchanged) |
| **Companion** | [implementation-plan.md](implementation-plan.md) |
| **Priority key** | **P0** = MVP (Phases 1–2) · **P1** = v1.0 (Phases 3–4) · **P2** = later (Phase 5+) |

---

## 0. Review of the original vision

### 0.1 What the original gets right (kept)

- **The right abstraction for users.** People deploy *AI Applications* (AI Accountant, AI Support Desk, AI Developer…), not agents. The runtime manages agents, models, memory, tools and workflows underneath, which is exactly what an operating system does for programs.
- **Event-driven core.** `publish(event)` instead of direct calls makes every action observable, replayable and decoupled.
- **Governance in the kernel.** Security, human approval and observability are OS components, not add-ons. This is where most agent frameworks are weakest and where enterprise buyers pay.
- **Pragmatic infrastructure path.** Start on PostgreSQL, move to NATS, consider Kafka last.
- **Provider independence** for models, memory backends and tools.

### 0.2 Gaps found, and where this version addresses them

| # | Gap in the original | Why it matters | Addressed in |
|---|---|---|---|
| 1 | "AI Application" is the headline concept but is never defined: no manifest, packaging, install flow, permissions or lifecycle. | It is the unit users deploy and pay for. Without it, AIOS is just another agent framework. | §5.1, §7.4 |
| 2 | Event semantics are unspecified: no envelope, delivery guarantee, ordering, idempotency, schema versioning, retries or dead-letter handling. | These decide whether an event-driven system is reliable or a source of duplicated actions and lost work. | §5.2, §7.1–7.2 |
| 3 | Pure event choreography across 1,000+ agents has no guard against event storms, agents triggering each other in loops, or chains nobody can trace. | Agents that trigger each other can loop and burn budget in minutes. | §3 (P2, P5), FR-EVT-11 |
| 4 | The Phase 1 bus relies on PostgreSQL `LISTEN/NOTIFY`, which is not durable: notifications are lost when no listener is connected, and payloads are capped at 8 KB. | Lost events mean lost or duplicated work. | §8.1 |
| 5 | The security kernel omits AI-specific threats: prompt injection, tool/MCP poisoning, memory poisoning, data exfiltration, confused-deputy delegation, runaway cost. | These are the main ways agent systems actually get breached. | §5.8 |
| 6 | Memory mixes two dimensions (who can see an item vs. how long it lives); `put` / `search` / `recall` have no defined semantics; there is no forgetting, provenance or GDPR erasure. | Ambiguous scopes cause cross-user leaks; missing erasure blocks regulated buyers. | §5.5, §7.7 |
| 7 | Model routing is hard-coded to vendor names (`coding → Claude`, `reasoning → GPT`). | Model rankings change every few months. Routing must be configuration validated by evals, with fallbacks, budgets and data-classification guards. | §5.6, §7.10 |
| 8 | The architecture diagram omits the Model Kernel, Tool OS, Security Kernel, Human OS and Observability. | Engineers build from the diagram. | §4 |
| 9 | No personas, goals, non-goals, non-functional requirements, MVP scope or success metrics. | Without them, scope can't be controlled and progress can't be measured. | §1, §6, §10 |
| 10 | Five products are listed with no order, and several overlap heavily. | Building five products at once is the likeliest way to ship none. | §9 |
| 11 | Missing entirely: control-plane API, CLI and SDKs, local developer experience, agent evaluation and testing, cost management, interoperability (A2A, inbound MCP, OpenAI-compatible API) and the deployment model. | Developers and enterprises need each of these before they can adopt the platform. | §5.10–5.13 |
| 12 | The landscape moved in 2025–26: hyperscalers launched agent runtimes, CNCF gained agent projects, and MCP and A2A now sit under one foundation. | Positioning has to reflect today's competition and standards. | §1.6 |

The copy-paste artefacts in the original draft (stray `Plain Text` / `Python` labels, the `agent.call_tool()` / `tool.call_agent()` line, "echnology Stack") are resolved here.

---

## 1. Vision and positioning

### 1.1 Problem

Teams building agentic systems today hand-assemble an agent framework, a model gateway, a vector database, a workflow engine, a tracing tool and custom security code. Every AI application re-implements lifecycle, memory, tool credentials, approvals and audit. The result is prototypes that fail security review, can't be debugged when they misbehave, and are locked to one model vendor or one cloud.

### 1.2 Vision

> **Windows/Linux is the operating system for applications. AIOS is the operating system for agents.**
>
> AIOS schedules agents, mediates every model, tool and memory access through policy, records every action as an event, and lets organizations install packaged **AI Applications** the way they install software. One runtime: any model, any agent, any tool, any workflow.

### 1.3 The OS analogy as a design compass

| Operating system | AIOS equivalent |
|---|---|
| Program / package | **AI App** (App manifest + agents + workflows + policies) |
| Process | **Agent instance** |
| Process manager / scheduler | **Agent Kernel** |
| System call | Tool invocation, model call, memory access — all mediated by the runtime |
| IPC / message queues | **Event Fabric** |
| Init system / job control | **Workflow Engine** |
| File system and page cache | **Memory OS** (scoped persistent memory and working memory) |
| Device drivers | Model adapters and tool providers |
| Users, groups, permissions, SELinux | **Security Kernel** (identity, RBAC/ABAC, policy) |
| `sudo` prompt | **Human OS** (approvals) |
| `/proc`, `strace`, syslog | **Observability** (event log, traces, audit) |
| Package manager and app permissions | App registry and permission consent at install |
| Resource limits (`ulimit`, cgroups) | Budgets: tokens, cost, steps, time, event hops |

### 1.4 Personas

| Persona | Needs from AIOS |
|---|---|
| **App builder** (developer) | Write agents in Python, compose workflows, test locally, debug runs, ship versions safely. |
| **Platform operator** (SRE / platform team) | Install and run AIOS on their own infrastructure; scale, upgrade, monitor and control cost. |
| **Tenant admin** | Install apps, grant permissions, connect tools and data, set budgets and policies. |
| **End user** | Use an AI App through chat, API or integrations and get reliable, fast results. |
| **Approver** | Review proposed high-risk actions with full context and decide quickly from any channel. |
| **Security / compliance officer** | Prove who did what, why and with which data; enforce policy; satisfy audit and regulation. |

### 1.5 Goals and non-goals

**Goals**

| ID | Goal | Measured by |
|---|---|---|
| G1 | A developer builds, runs and debugs a multi-agent AI App locally in under one day. | Design-partner onboarding time |
| G2 | Every action in a run (model call, tool call, memory access, approval) is recorded, attributable and replayable. | 100% event coverage in reference-app traces |
| G3 | Swapping a model provider, vector store or message broker is a configuration change, not a code change. | Adapter conformance suites |
| G4 | No agent performs an action that its own policy *and* its human principal don't both allow. | Security test suite and red-team results |
| G5 | The same App runs unchanged on a laptop, a single VM and Kubernetes. | End-to-end suite on all three targets |
| G6 | Policy-based model routing lowers cost per successful run versus a single-premium-model baseline at equal quality. | Cost/quality benchmark; target set from Phase 1 data |

**Non-goals**

- Training or fine-tuning models.
- Inventing another agent reasoning framework. AIOS *hosts* agents, including ones built with LangGraph, CrewAI, OpenAI Agents SDK, Google ADK or Microsoft Agent Framework.
- Competing with Kafka or NATS as a general-purpose broker. The broker is pluggable; the agent semantics on top are the product.
- Replacing Kubernetes. AIOS runs on it, and without it.
- A rich end-user chat experience beyond the reference console. Apps bring their own front ends.

### 1.6 Landscape and differentiation (as of October 2026)

| Category | Examples | Overlap with AIOS | How AIOS differs |
|---|---|---|---|
| Agent frameworks | LangGraph, CrewAI, OpenAI Agents SDK, Google ADK, Microsoft Agent Framework | Agent authoring and orchestration | AIOS hosts them through adapters and adds lifecycle, policy, memory and audit around them. |
| Durable execution | Temporal, Restate, Inngest, Dapr Workflow | Workflow Engine | Agent-native semantics (capabilities, budgets, approvals, model and tool events) on one event log. |
| Managed agent runtimes | AWS Bedrock AgentCore, Azure AI Foundry Agent Service, Google Vertex AI Agent Engine | Closest to the full vision | Cloud-neutral and self-hostable, including on-prem for regulated industries; any model. |
| Open-source cloud-native agent runtimes | Dapr Agents (v1.0: durable workflows, SPIFFE identity), kagent (CNCF sandbox: Kubernetes CRDs for agents), Kubernetes Agent Sandbox (SIG Apps) | "Agent Kubernetes" | The App abstraction, event-sourced replay and policy-first design; also runs without Kubernetes. |
| LLM gateways | LiteLLM, Portkey, OpenRouter | Model Kernel | Routing tied to policy, data classification and run budgets, not just traffic. |
| Agent memory | Mem0, Zep / Graphiti, Letta | Memory OS | Scoped, policy-enforced, provider-independent memory with event provenance and erasure. |
| LLM observability | Langfuse, LangSmith, Arize Phoenix | Observability | The trace *is* the event log, so runs can be replayed and forked, not just viewed. |

**Standards to build on, not reinvent:** MCP (agent ↔ tools) and A2A (agent ↔ agent), both now governed by the Linux Foundation's Agentic AI Foundation; CloudEvents for the event envelope; OpenTelemetry and its GenAI semantic conventions (still in *Development* status in 2026, so pin a version); OpenAPI and AsyncAPI; OIDC.

**Where AIOS must win.** The space is crowded, so the thesis has to be sharp:

1. **Neutrality.** Any model, any cloud, on-prem. Hyperscaler runtimes favor their own cloud.
2. **Event-sourced replay and audit.** Every run can be reproduced, forked and proven. This is structural and hard for others to bolt on later.
3. **The AI App as a governed unit.** Packaged apps with declared permissions, budgets and policies, installed per tenant with admin consent.
4. **Policy first.** Default-deny enforcement at every boundary, including AI-specific threats.

> **Validate before Phase 2:** interview 10–15 target organizations (banks, healthcare, regulated SaaS) to confirm that points 1–4 drive purchases. Adjust §9 if they don't.

> **Naming note:** "AIOS" is already used by an open-source research project ("AIOS: LLM Agent Operating System", `agiresearch/AIOS` on GitHub). Check trademarks and search conflicts before public branding.

---

## 2. Core concepts (glossary)

| Term | Definition |
|---|---|
| **AI App** | The deployable product unit: a versioned bundle of agents, workflows, tools, memory configuration, policies, model policy, config schema, budgets and entry points (§7.4). |
| **Tenant** | An isolated customer or organizational unit. Every record, event, memory item and trace carries a tenant id. |
| **Agent** | A versioned definition of an autonomous worker that provides one or more *capabilities* (§7.3). |
| **Agent instance** | A running activation of an agent that handles tasks. Stateless by default. |
| **Capability** | A named, typed unit of work an agent can perform (e.g., `research.web`), with input and output schemas. Work is addressed to capabilities, not to specific agents. |
| **Event** | An immutable command or fact in the AIOS envelope (§7.1). |
| **Command** | An event that asks for work (`*.requested`). Exactly one handler consumes it. |
| **Fact** | An event that records something that happened (past tense). Broadcast to all subscribers. |
| **Topic** | The hierarchical name an event is published under (§7.2). |
| **Workflow** | A versioned graph of steps (§5.4, §7.6). |
| **Run** | One execution of a workflow, with an id, a budget and a complete event history. |
| **Step / task** | A step is a node in a workflow; a task is the unit of work the kernel dispatches to an agent for that step. |
| **Tool** | A typed operation that reads data or has side effects (e.g., `github.pr.create`), served by a tool provider (native, MCP, OpenAPI, A2A). |
| **Memory scope** | Who can see a memory item: working, session, user, agent, app, tenant or global (§5.5). |
| **Principal** | Any identity that acts: user, service account, agent or app. |
| **Policy** | A versioned rule set that the Security Kernel evaluates on every governed action. |
| **Approval** | A durable human decision point that pauses a run. |
| **Budget** | Hard and soft limits on tokens, cost, steps, wall-clock time and event hops. |

---

## 3. Architectural principles

| # | Principle | What it means in practice |
|---|---|---|
| P1 | **Everything is an event.** | The event log is the source of truth. Developers write ergonomic calls (`ctx.tools.invoke(...)`) and the runtime records each one as events. Every call is *recorded*; it may be *transported* in-process when that is faster and policy allows. |
| P2 | **Orchestrate with workflows, communicate with events.** | Workflows give each run a clear owner and shape; events carry the work. Free-form choreography is allowed for side reactions (indexing, notifications), not for a run's main path. This keeps 1,000-agent systems traceable. |
| P3 | **Address capabilities, not agents.** | `task.requested` with `capability: research.web` goes to any healthy agent that provides it. This is the practical meaning of "semantic topics". |
| P4 | **Default deny at every boundary.** | Publish, subscribe, dispatch, tool, memory, model and admin actions all pass through the Security Kernel. |
| P5 | **Bounded autonomy.** | Every run and task has budgets (tokens, cost, steps, time, hops). Nothing runs unbounded. |
| P6 | **Ports and adapters.** | Brokers, databases, vector stores, model providers and tool providers sit behind stable interfaces with shared conformance test suites. |
| P7 | **Always replayable, deterministic where possible.** | Model and tool outputs are recorded, so a run can be replayed exactly and forked from any step. |
| P8 | **Untrusted by default.** | Tool outputs, retrieved documents and user input are tagged untrusted and can't silently drive high-risk actions. |
| P9 | **Open standards first.** | CloudEvents, MCP, A2A, OpenTelemetry, OpenAPI/AsyncAPI, OIDC. |
| P10 | **Local first.** | The whole OS runs on a laptop with one command; production differs by configuration, not code. |

---

## 4. Reference architecture

```
 AI Apps           AI Accountant · AI Support Desk · AI Developer · AI Researcher · AI Architect
    │
 Access layer      REST + streaming API · OpenAI-compatible API · MCP server · A2A server
    │              CLI · Console · Webhooks
    │
 Control plane     App registry · Manifests · Reconciler · Tenancy · Quotas
    │
 Runtime           Agent Kernel │ Workflow Engine │ Human OS │ Memory OS │ Model Kernel │ Tool OS
 (data plane)      ═══ Security Kernel — policy enforcement at every boundary ═══════════════
                   ═══ Observability — events · traces · metrics · audit · evals ═════════════
    │
 Event Fabric      EventBus port → PostgreSQL (MVP) │ NATS JetStream (v1.0) │ Kafka (optional)
    │
 Persistence       PostgreSQL (events, state, row-level security) · pgvector / Qdrant
    │              Object storage (artifacts, archives) · Secrets vault
    │
 Infrastructure    Laptop (Docker Compose) · Single VM · Kubernetes · Any cloud or on-prem
```

The control plane stores desired state (which apps, agents, workflows and policies exist). The runtime executes runs. Security and observability cut across every runtime component.

### 4.1 How a request flows (AI Researcher example)

The original chain (`USER_REQUEST → PLAN_CREATED → … → RESPONSE_READY`) maps to standard AIOS events:

```
app.request.received                     access layer accepts the user's question
run.started                              workflow engine starts research-report v1.0.0
  task.requested   cap=planning          → planner-agent
    model.call.completed                 tokens, cost, model chosen by policy
  task.completed   → research.plan.created
  task.requested   cap=research.web ×3   → research-agent instances, in parallel
    memory.recalled
    tool.call.requested → policy decision → tool.call.completed
  task.completed ×3 → research.completed
  task.requested   cap=writing           → writer-agent
  task.requested   cap=review            → reviewer-agent → research.review.completed
run.completed
app.response.ready                       response streamed to the user
```

Every line is a persisted event with the same `runid` and `correlationid`, so the Trace Explorer can draw the run as a DAG and replay it.

---

## 5. Functional requirements

### 5.1 AI Application model *(new)*

| ID | Requirement | Priority |
|---|---|---|
| FR-APP-01 | An App manifest (§7.4) declares agents, workflows, tools, memory configuration, policies, model policy, config schema, budgets and entry points in one bundle. | P0 |
| FR-APP-02 | Entry points: chat (REST/WebSocket) and event triggers (P0); schedules, webhooks, OpenAI-compatible API and A2A (P1). | P0/P1 |
| FR-APP-03 | Installing an App into a tenant lists the permissions it requests (tools, memory scopes, data classes, budgets). A tenant admin must consent, as with mobile app permissions, and permissions not granted are enforced at runtime. | P1 |
| FR-APP-04 | Versioned upgrade and rollback; in-flight runs finish on the version they started with. | P1 |
| FR-APP-05 | Uninstall with an explicit data choice: retain, export or delete. | P1 |
| FR-APP-06 | Apps are packaged as OCI artifacts and published to a private registry (P1); public marketplace (P2). | P1/P2 |
| FR-APP-07 | Per-install configuration and secret bindings are validated against the App's config schema before activation. | P0 |
| FR-APP-08 | Environments per install (dev, staging, prod) with promotion between them. | P2 |

### 5.2 Event Fabric

| ID | Requirement | Priority |
|---|---|---|
| FR-EVT-01 | All communication between runtime components uses events in the AIOS envelope (CloudEvents 1.0 plus AIOS extensions, §7.1). | P0 |
| FR-EVT-02 | Commands (`*.requested`, consumed by exactly one handler) are distinct from facts (past tense, broadcast to all subscribers). | P0 |
| FR-EVT-03 | Hierarchical topic grammar with NATS-compatible wildcards: `*` matches one token, `>` matches all remaining tokens (§7.2). | P0 |
| FR-EVT-04 | The fabric applies tenant and app namespacing. Agent code never builds physical subjects and can't subscribe across tenants. | P0 |
| FR-EVT-05 | At-least-once delivery; competing consumer groups and broadcast subscriptions; ordering guaranteed per partition key (default: `runid`). | P0 |
| FR-EVT-06 | Every event has a unique id; consumers deduplicate through an inbox keyed by event id or idempotency key. The SDK does this automatically. | P0 |
| FR-EVT-07 | Transactional publish: on the PostgreSQL backend, state changes and their events commit atomically (outbox pattern). | P0 |
| FR-EVT-08 | Retries with exponential backoff and jitter up to a maximum number of attempts, then a dead-letter queue with inspect, replay and discard tooling. | P0 |
| FR-EVT-09 | Replay by position, time range or run, into a new consumer group or a sandbox with side effects disabled. | P0 |
| FR-EVT-10 | Schemas: a JSON Schema per event type and version, validation on publish and backward-compatibility checks in CI (P0); a schema registry service with an API (P1). | P0/P1 |
| FR-EVT-11 | Loop and storm protection: each causally derived event increments `hopcount`; publishing beyond the limit (default 64) is rejected with `budget.exceeded`; per-run event rate limits apply. | P0 |
| FR-EVT-12 | A request/reply helper over events (correlation id plus timeout) for call-style APIs. | P0 |
| FR-EVT-13 | Payloads above 256 KB go to object storage and the event carries a reference (claim-check pattern). | P1 |
| FR-EVT-14 | Retention policy per event domain, archival to object storage and legal hold. | P1 |
| FR-EVT-15 | Backends behind one `EventBus` port: in-memory and PostgreSQL (P0), NATS JetStream (P1), Kafka/Redpanda (P2). Every backend passes one conformance suite. | P0–P2 |
| FR-EVT-16 | Capability-based routing: a `task.requested` event carries a capability and is dispatched to a healthy agent that provides it (P0). Intent matching for unregistered capability names via embeddings, with a confidence threshold and a policy check (P2). | P0/P2 |
| FR-EVT-17 | Model token streams travel over a separate streaming channel (SSE/WebSocket), not the fabric; only summary events such as `model.call.completed` are published. | P0 |

### 5.3 Agent Kernel

| ID | Requirement | Priority |
|---|---|---|
| FR-AGT-01 | An Agent manifest (§7.3) declares identity, version, capabilities with typed input and output schemas, subscriptions, runtime type, allowed tools, memory scopes, model requirements, limits, scaling and isolation level. | P0 |
| FR-AGT-02 | Runtime types: native Python via the SDK (P0); container image, remote A2A agent, and framework adapters such as LangGraph and OpenAI Agents SDK (P1). | P0/P1 |
| FR-AGT-03 | A lifecycle state machine — `Registered → Starting → Ready ⇄ Busy → Suspended → Draining → Stopped`, plus `Failed` — publishes every transition as `agent.lifecycle.*`. Operations: start, stop, suspend, resume, scale, drain, restart. | P0 |
| FR-AGT-04 | Supervision with restart policies (`Always`, `OnFailure`, `Never`) and backoff; a crashing agent can't take down its worker. | P0 |
| FR-AGT-05 | Dispatch by capability, health and load, with per-agent concurrency limits (P0); placement by labels such as GPU, region or sandbox (P1). | P0/P1 |
| FR-AGT-06 | Scaling: minimum and maximum instances, scale to zero, and autoscaling on queue depth or latency. | P1 |
| FR-AGT-07 | Agents are stateless by default, with state held in the run and in Memory OS (P0). Optional stateful "virtual actor" agents with a single activation per key (P2). | P0/P2 |
| FR-AGT-08 | Per-task limits on steps, tokens, cost and wall-clock time; exceeding one fails the task with `budget.exceeded`. | P0 |
| FR-AGT-09 | Isolation levels: in-process for trusted first-party agents (P0); subprocess, container, and gVisor or micro-VM sandbox for untrusted code execution (P1). | P0/P1 |
| FR-AGT-10 | Each agent has its own workload identity for policy and audit. No shared superuser identity. | P0 |
| FR-AGT-11 | Several versions of an agent run side by side, with canary traffic splitting and rollback. | P1 |
| FR-AGT-12 | Liveness and readiness checks plus heartbeats; unhealthy instances are removed from dispatch. | P0 |
| FR-AGT-13 | Hot reload of agent code and manifests in dev mode. | P0 |

**Scale model.** Expect 1,000+ registered agent definitions per tenant. Definitions are cheap. Instances are activated on demand, many agents share one worker process, and idle agents cost close to nothing.

### 5.4 Workflow Engine

| ID | Requirement | Priority |
|---|---|---|
| FR-WF-01 | Workflows are written in a Python DSL (P0) or YAML/JSON (P1). Both compile to one versioned graph IR, which the visual designer (P2) also edits. | P0–P2 |
| FR-WF-02 | Node types: agent task (by capability), tool call, model call, human approval or task, condition, parallel fan-out and join (all / any / n-of-m), bounded loop, sub-workflow, and wait (timer, event or signal) (P0); compensation (P1). | P0/P1 |
| FR-WF-03 | Dynamic planning: a planner agent may emit tasks or a sub-graph at runtime. The engine validates it against schema, allowed capabilities, policy and remaining budget, then expands it (e.g., into a parallel fan-out). | P0 |
| FR-WF-04 | Durable execution: every state transition is an event, run state is a fold over those events, and after a crash a run resumes from its last committed step without re-executing completed steps, because their outputs are recorded. | P0 |
| FR-WF-05 | Per-step retry policy, timeout and error routing (`on_error` edges). | P0 |
| FR-WF-06 | Saga-style compensation for steps with external side effects. | P1 |
| FR-WF-07 | Waits of hours to weeks consume no compute; timers are durable. | P0 |
| FR-WF-08 | Signals (external input into a running run) and queries (read its current state). | P0 |
| FR-WF-09 | Runs pin the definition version they started with; new runs use the latest version. | P0 |
| FR-WF-10 | Run-level budgets (tokens, cost, steps, wall-clock, hops) enforced across all steps. | P0 |
| FR-WF-11 | Run control: start, cancel, pause and resume (P0); retry from a step and fork from a step (P1). | P0/P1 |
| FR-WF-12 | Typed step outputs passed by reference expressions; large artifacts passed by reference to object storage. | P0 |
| FR-WF-13 | Concurrency limits per workflow and per tenant; queued runs with priorities. | P1 |
| FR-WF-14 | Optional Temporal backend adapter for organizations standardized on Temporal. | P2 |

**Design note.** The engine executes *graphs* whose step results are recorded. It does not replay arbitrary code deterministically the way Temporal does. That is simpler to build and reason about, sufficient for agent workflows, and maps directly onto the event log (ADR-006).

### 5.5 Memory OS

The original's six memory types mix two independent dimensions. This version separates them.

**Dimension 1 — Scope (who can see it):**

| Scope | Visible to | Typical lifetime | Example |
|---|---|---|---|
| Working | One run | Run duration | Intermediate results, scratchpad |
| Session | One conversation or session | Session plus TTL | Chat history, current ticket context |
| User | One end user, across sessions and the apps that user authorizes | Until deleted | Preferences, summarized history |
| Agent | One agent definition within a tenant | Long-lived | Lessons learned, procedures that worked |
| App | One App install | Long-lived | The App's knowledge base |
| Tenant | The whole tenant, subject to policy | Governed retention | Company documents, policies, glossaries |
| Global | All tenants, read-only, curated by the operator | Long-lived | Public reference knowledge |

**Dimension 2 — Retention (how long it lives):** `ephemeral` (in memory only), `ttl` (expires), `persistent`, `legal_hold`. The original's *Short Memory* is working or session scope with ephemeral or TTL retention; *Long Memory* is persistent retention in any scope.

**Memory kinds:** *episodic* (what happened, derived from the event log at no extra cost), *semantic* (facts and documents) and *procedural* (playbooks, prompts and tool sequences that worked).

**Operations:**

| Operation | Semantics |
|---|---|
| `put` | Write an item into one scope with kind, retention, sensitivity and provenance. Policy-checked. |
| `search` | Raw retrieval: ranked items from the given scopes for a query (hybrid vector + keyword + filters). |
| `recall` | Context assembly for a task: multi-scope search, reranking, deduplication, compression and citations, fitted to a token budget. What agents normally call. |
| `forget` | Erase by id, filter or subject (e.g., a user), including embeddings, caches and derived summaries. Emits an erasure record. |
| `get` / `update` | Direct, versioned access by id. |

| ID | Requirement | Priority |
|---|---|---|
| FR-MEM-01 | The seven scopes and four retention classes above. | P0 |
| FR-MEM-02 | `put`, `search`, `recall`, `forget`, `get` and `update` with identical semantics on every backend (§7.7). | P0 |
| FR-MEM-03 | `recall` builds a context pack for a task within a token budget: multi-scope hybrid retrieval (vector, keyword, recency, importance), reranking, deduplication, optional compression and citations. | P0 |
| FR-MEM-04 | Every item carries provenance (source event id, author principal, time, source URI) and a sensitivity label. | P0 |
| FR-MEM-05 | The Security Kernel checks every read and write. Write permission is granted per scope, which limits memory poisoning. | P0 |
| FR-MEM-06 | Tenant isolation in storage (a namespace per tenant plus row-level security); similarity search never crosses tenants. | P0 |
| FR-MEM-07 | Document ingestion with chunking, embedding, incremental re-sync and deletion propagation, for files and URLs (P0); S3, SharePoint, Confluence and Git connectors (P1). | P0/P1 |
| FR-MEM-08 | An embedding model registry; each item records its embedding model version, and a background job re-indexes when the model changes. | P1 |
| FR-MEM-09 | Consolidation jobs summarize sessions into user and agent memory, extract facts and expire low-value items. Every consolidation is attributable and reversible. | P1 |
| FR-MEM-10 | Erasure: `forget(subject=…)` deletes items and embeddings (P0) plus caches and derived summaries (P1), and records an erasure event. | P0/P1 |
| FR-MEM-11 | Backends through adapters: pgvector (P0); Qdrant (P1); Weaviate, Pinecone, Azure AI Search and graph memory (P2). All pass one conformance suite. | P0–P2 |
| FR-MEM-12 | Retrieval quality evaluation (recall@k, MRR, groundedness) on golden datasets. | P1 |
| FR-MEM-13 | Retrieved content is tagged untrusted for injection defenses (FR-SEC-09). | P0 |

### 5.6 Model Kernel

| ID | Requirement | Priority |
|---|---|---|
| FR-MOD-01 | One interface for generation, tool calling, structured output (Pydantic / JSON Schema), embeddings and streaming that normalizes provider differences (P0); multimodal input (P1). | P0/P1 |
| FR-MOD-02 | Provider adapters: Anthropic, OpenAI, Google Gemini, and any OpenAI-compatible endpoint, which covers Llama via vLLM or Ollama, Mistral and DeepSeek (P0); AWS Bedrock, Azure OpenAI and Google Vertex AI (P1). | P0/P1 |
| FR-MOD-03 | A model registry held as configuration: capabilities, context window, prices, latency profile, deployment type (public API or private), region and compliance flags. | P0 |
| FR-MOD-04 | Declarative routing policies (§7.10) match on task class, data classification, tenant tier, budget, latency needs and required capabilities, and produce an ordered list of candidate models. | P0 |
| FR-MOD-05 | Reliability: retries with backoff, provider fallback chains and timeouts (P0); circuit breakers, rate-limit-aware queueing and load balancing across keys and regions (P1). | P0/P1 |
| FR-MOD-06 | Data-classification guard: `restricted` content is only ever routed to approved deployments (private, or contracted zero-retention). | P0 |
| FR-MOD-07 | Token and cost accounting for every call, attributed to tenant, App, agent, run and user, and published as `model.call.completed`. | P0 |
| FR-MOD-08 | Hard and soft budgets per run, App, tenant and user. A soft limit raises an alert and downgrades the route; a hard limit denies the call. | P0 |
| FR-MOD-09 | Caching: provider prompt caching (P0); an exact-match response cache and an opt-in, tenant-isolated semantic cache (P1). | P0/P1 |
| FR-MOD-10 | Guardrail hooks before and after each call (P0); built-in PII redaction, injection detection, output validation and moderation guardrails (P1). | P0/P1 |
| FR-MOD-11 | Adaptive routing driven by eval scores, cost and latency telemetry, e.g., try a small model first and escalate on low confidence. | P2 |
| FR-MOD-12 | Record/replay mode for deterministic tests and run replay. | P0 |

**On the original routing examples.** "Coding → Claude, reasoning → GPT, cheap → Llama" is a sensible starting configuration, but model strengths change every few months. The task-to-model mapping is configuration that evals validate (§7.10), never code. The "huge savings" claim becomes goal G6, a measured hypothesis.

### 5.7 Tool OS

| ID | Requirement | Priority |
|---|---|---|
| FR-TOOL-01 | A tool registry with qualified names `<provider>.<resource>.<action>` (e.g., `github.pr.create`), JSON Schema input and output, description, version and side-effect class. | P0 |
| FR-TOOL-02 | Side-effect classes `read`, `write`, `destructive`, `external_comm`, `financial` and `code_exec`; policies and approvals key off them. | P0 |
| FR-TOOL-03 | Logical-to-physical resolution: `tool.invoke("filesystem.file.read")` resolves to a provider binding (P0), selectable per tenant and environment, e.g., local disk in dev and object storage in production (P1). | P0/P1 |
| FR-TOOL-04 | Provider types: native Python, MCP servers (stdio and streamable HTTP, importing tools, resources and prompts), and HTTP and filesystem built-ins (P0); OpenAPI import plus GitHub, Jira and database connectors (P1); Kubernetes, AWS and Azure connectors (P2). A2A agents are covered by FR-INT-02. | P0–P2 |
| FR-TOOL-05 | Invocation pipeline: validate input → check policy → request approval if required → inject credentials → execute in the isolation the side-effect class requires → validate and sanitize output → tag it untrusted → emit events. | P0 |
| FR-TOOL-06 | Credentials: service credentials (P0) and user-delegated OAuth on-behalf-of (P1). Secrets never enter prompts, events or logs. | P0/P1 |
| FR-TOOL-07 | Idempotency keys for write tools (P0); dry-run previews where the provider supports them, shown in approval requests (P1). | P0/P1 |
| FR-TOOL-08 | Per-tool timeouts (P0); rate limits, concurrency limits and cost attribution (P1). | P0/P1 |
| FR-TOOL-09 | MCP supply-chain safety: a server allowlist and pinning tool descriptions by hash, alerting or blocking when a description changes (P0); trust tiers and per-server egress restrictions (P1). | P0/P1 |

### 5.8 Security Kernel

**Threat model (AI-specific additions to classic application security):**

| Threat | Example | Control |
|---|---|---|
| Direct prompt injection | A user tells the agent to ignore its rules | Policy is enforced outside the model; the model can't grant itself permissions (FR-SEC-04) |
| Indirect prompt injection | A web page or KB article tells the agent to issue a refund | Taint tracking, plus approval for high-risk tools while context is untrusted (FR-SEC-09) |
| Tool / MCP poisoning ("rug pull") | An MCP server changes a tool description to exfiltrate data | Server allowlist and description pinning (FR-TOOL-09) |
| Memory poisoning | An attacker plants false "facts" in tenant memory | Per-scope write permissions and provenance (FR-MEM-04, FR-MEM-05) |
| Data exfiltration | An agent encodes secrets in a URL it fetches | Egress allowlists, secrets kept out of context, DLP (FR-SEC-07, FR-SEC-10, FR-SEC-11) |
| Confused deputy | A low-privilege user drives a high-privilege agent | Effective permission = agent ∩ user (FR-SEC-05) |
| Cross-tenant leakage | Similarity search returns another tenant's data | Storage-level isolation and an isolation test suite (FR-SEC-06, FR-MEM-06) |
| Excessive agency, runaway loops | Agents trigger each other endlessly and burn budget | Budgets, hop limits, kill switch (FR-EVT-11, FR-AGT-08, FR-SEC-13) |
| Untrusted code execution | Generated code escapes its container | Sandbox isolation level (FR-AGT-09) |

The security test suite maps these to the OWASP Top 10 for LLM Applications and OWASP's agentic AI threat guidance.

| ID | Requirement | Priority |
|---|---|---|
| FR-SEC-01 | Tenancy hierarchy: Organization → Tenant → App install → Environment. The tenant id is on every record, event, memory item and trace. | P0 |
| FR-SEC-02 | Identities for all principals: users via OIDC, service accounts via API keys, and agents via workload identities (P0); mTLS with SPIFFE-compatible ids (P1); SAML and SCIM (P2). | P0–P2 |
| FR-SEC-03 | Authorization through an embeddable policy engine (Cedar, decided in ADR-007) that supports RBAC and ABAC. Default deny. Policies are versioned, tested and reviewed like code. | P0 |
| FR-SEC-04 | Policy enforcement points on publish/subscribe, task dispatch, tool invocation, memory read/write, model routing, approval decisions and every control-plane API. The model can't grant itself permissions. | P0 |
| FR-SEC-05 | Delegation: an agent acting for a user gets the intersection of its own permissions and the user's. The whole delegation chain is recorded on events and in the audit log. | P0 |
| FR-SEC-06 | Storage isolation: PostgreSQL row-level security by tenant, per-tenant memory namespaces and per-tenant topic prefixes. An automated cross-tenant isolation suite runs in CI. | P0 |
| FR-SEC-07 | Secrets through a pluggable vault: a local encrypted store and HashiCorp Vault/OpenBao (P0); AWS, Azure and GCP secret managers (P1). Short-lived credentials are injected only at execution time, and secret patterns are redacted from events, logs and traces. | P0/P1 |
| FR-SEC-08 | An append-only, hash-chained audit log of policy decisions, tool calls, approvals, admin changes and data access, with a verification tool (P0) and SIEM export (P1). | P0/P1 |
| FR-SEC-09 | Prompt-injection defense in depth: provenance and taint tracking of untrusted content. While a run's context contains untrusted content, `destructive`, `external_comm`, `financial` and `code_exec` tools require approval (configurable per policy). Pluggable injection classifiers; system instructions kept separate from data. | P0 |
| FR-SEC-10 | Egress control: allowlisted destinations per App and tool; requests that carry data to hosts not on the list are blocked. | P0 |
| FR-SEC-11 | Data classification labels (`public`, `internal`, `confidential`, `restricted`) propagate from sources to memory, events and model routing (P0); DLP scanning (P1). | P0/P1 |
| FR-SEC-12 | TLS in transit and encryption at rest (P0); per-tenant keys and BYOK (P2). | P0/P2 |
| FR-SEC-13 | Kill switch: suspend an agent, App or tenant immediately and revoke its credentials. | P0 |

### 5.9 Human OS

| ID | Requirement | Priority |
|---|---|---|
| FR-HUM-01 | Approval exists both as an explicit workflow node and as a policy outcome (`requireApproval`) on any governed action. | P0 |
| FR-HUM-02 | Events: `approval.requested`, `approval.granted`, `approval.denied`, `approval.modified`, `approval.expired`, `approval.escalated`, `approval.cancelled`. | P0 |
| FR-HUM-03 | Each request shows the action summary, the exact payload or diff, the risk level, the policy that triggered it, the agent's stated rationale and a link to the trace. | P0 |
| FR-HUM-04 | Routing to users or roles, with separation of duties: whoever initiated an action can't approve it (P0). Groups and n-of-m quorum (P1). | P0/P1 |
| FR-HUM-05 | Timeouts with explicit behavior: deny by default, auto-approve only where policy allows (P0); reminders and escalation chains (P1). | P0/P1 |
| FR-HUM-06 | Approve with edits: the approver changes parameters, and the modified action is re-checked against policy. | P1 |
| FR-HUM-07 | Channels: console inbox and REST/webhook (P0); Slack, Microsoft Teams and email with signed action links (P1); mobile push (P2). | P0–P2 |
| FR-HUM-08 | Humans as capability providers: users or groups registered for a capability (e.g., `legal.review`) receive `human.task.*` work exactly as agents receive tasks. | P1 |
| FR-HUM-09 | Feedback capture (ratings, corrections) linked to the run and step, feeding evals and memory. | P1 |
| FR-HUM-10 | Paused runs consume no compute and survive restarts and deployments. | P0 |

### 5.10 Observability and evaluation

| ID | Requirement | Priority |
|---|---|---|
| FR-OBS-01 | Every event is persisted and can be queried by run, tenant, App, agent, type and time. | P0 |
| FR-OBS-02 | OpenTelemetry tracing across all components. W3C trace context travels in the event envelope. Model, tool, retrieval and agent spans use GenAI semantic-convention attributes through one internal mapping module pinned to a convention version. | P0 |
| FR-OBS-03 | Trace Explorer: a run shown as a DAG and timeline, with per-node inputs and outputs (redacted per policy), tokens, cost, latency, policy decisions, memory reads and approvals. Terminal view (P0); console view (P1). | P0/P1 |
| FR-OBS-04 | Replay and fork: re-render a past run exactly from its recorded events, or fork from any step with a changed input, prompt, model or agent version. Side-effecting tools are stubbed by default, and outcomes can be compared side by side. | P1 |
| FR-OBS-05 | Metrics: run outcomes, latency (including time to first token), tokens, cost, cache hit rate, approval wait time, policy denials, consumer lag and dead-letter depth, exported via Prometheus and OTLP. | P0 |
| FR-OBS-06 | Cost explorer by tenant, App, agent, model and user, with budget alerts. | P1 |
| FR-OBS-07 | Evaluation framework: datasets; deterministic, schema, LLM-as-judge and human graders; experiment comparison across versions; CI gating; sampled online evaluation of production runs. | P1 |
| FR-OBS-08 | Export to external tools (Jaeger, Tempo, Datadog, Langfuse, Phoenix and others) over OTLP. | P0 |
| FR-OBS-09 | Structured JSON logs correlated by run and trace ids, with PII and secret redaction. | P0 |
| FR-OBS-10 | Alerts on SLO breaches, budget thresholds, error spikes and dead-letter growth. | P1 |

### 5.11 Control plane, API, CLI and SDKs *(new)*

| ID | Requirement | Priority |
|---|---|---|
| FR-CP-01 | Declarative resources (`App`, `Agent`, `Workflow`, `Tool`, `ToolProvider`, `ModelProvider`, `ModelPolicy`, `Policy`, `MemoryStore`, `Tenant`) applied through the API or CLI (P0); a reconciler continuously converges actual state to desired state (P1). | P0/P1 |
| FR-CP-02 | A REST API described by OpenAPI 3.1, plus SSE/WebSocket streaming; the event catalog published as an AsyncAPI document. | P0 |
| FR-CP-03 | `aios` CLI: `init`, `dev`, `apply`, `get`, `run`, `logs`, `trace`, `events tail`, `dlq` (P0); `describe`, `replay`, `approvals`, `eval` (P1). | P0/P1 |
| FR-CP-04 | Python SDK for writing agents and as an API client (P0); TypeScript client SDK (P1). | P0/P1 |
| FR-CP-05 | Local dev mode: one command starts the full OS (API, worker, PostgreSQL with pgvector, OTel Collector, trace UI) with hot reload. | P0 |
| FR-CP-06 | Templates and scaffolding for Apps, agents and tools. | P1 |

### 5.12 Console (web UI) *(new)*

| ID | Requirement | Priority |
|---|---|---|
| FR-UI-01 | Apps: catalog, installed apps, configuration and permission consent. | P1 |
| FR-UI-02 | Agents: status, lifecycle controls, versions and metrics. | P1 |
| FR-UI-03 | Runs: search, live view, Trace Explorer, replay and fork. | P1 |
| FR-UI-04 | Approvals inbox with full context and approve / deny / modify actions: minimal (P0), complete (P1). | P0/P1 |
| FR-UI-05 | Cost and usage dashboards with budgets. | P1 |
| FR-UI-06 | Policy viewer and simulator ("would this action be allowed?"). | P1 |
| FR-UI-07 | Memory browser to inspect, edit and delete items, with audit. | P1 |
| FR-UI-08 | Visual workflow designer that edits the same graph IR. | P2 |
| FR-UI-09 | Event stream explorer and dead-letter queue management. | P1 |

### 5.13 Interoperability *(new)*

| ID | Requirement | Priority |
|---|---|---|
| FR-INT-01 | MCP client for tools, resources and prompts (P0); MCP server that exposes Apps and agents to external MCP clients such as IDEs and desktop assistants (P1). | P0/P1 |
| FR-INT-02 | A2A: publish Agent Cards for AIOS agents, use remote A2A agents as capability providers, and verify signed cards. | P1 |
| FR-INT-03 | An OpenAI-compatible chat endpoint per App, so existing clients and SDKs can talk to AI Apps. | P1 |
| FR-INT-04 | Framework adapters that host agents built with LangGraph and OpenAI Agents SDK (P1), and with CrewAI, Google ADK and Microsoft Agent Framework (P2). | P1/P2 |
| FR-INT-05 | Inbound and outbound webhooks; CloudEvents over HTTP for external event sources. | P1 |

---

## 6. Non-functional requirements

Targets are initial values. Phase 1 benchmarks confirm or revise them.

| ID | Category | Requirement and target |
|---|---|---|
| NFR-01 | Performance | Fabric publish-to-delivery latency on the PostgreSQL backend (single node): p50 ≤ 20 ms, p99 ≤ 100 ms at 1,000 events/s sustained. |
| NFR-02 | Performance | NATS JetStream backend: p99 ≤ 20 ms at 10,000 events/s. |
| NFR-03 | Overhead | AIOS adds ≤ 5% to end-to-end run latency compared with calling the same models and tools directly (model calls dominate run time). |
| NFR-04 | Scale | ≥ 1,000 agent definitions per tenant; ≥ 500 concurrently active runs and ≥ 100,000 paused runs on the v1.0 reference cluster; scale out by adding workers. |
| NFR-05 | Durability | No acknowledged event is ever lost (synchronous commit on a single node; synchronous replica in HA). HA mode: RPO = 0, RTO ≤ 15 minutes. |
| NFR-06 | Correctness | After a worker crash, completed steps are never re-executed, and a write tool executes at most once per idempotency key (effectively-once side effects). |
| NFR-07 | Availability | 99.9% monthly for API and runtime in an HA deployment (v1.0); best effort in single-node mode. |
| NFR-08 | Security | Default deny; zero cross-tenant access in the isolation suite; all secrets encrypted; audit log verifiable; every threat in §5.8 covered by automated tests. |
| NFR-09 | Privacy and compliance | GDPR erasure completes across all stores within 30 days (target: minutes for memory). Per-tenant data residency (P2). Record-keeping and human-oversight features suitable for EU AI Act high-risk obligations (confirm the applicable dates for each use case). SOC 2 control mapping by the Enterprise phase. |
| NFR-10 | Portability | Runs on Linux, macOS and Windows (WSL2/Docker) laptops, on any CNCF-conformant Kubernetes, and on all major clouds, with no mandatory proprietary cloud service. |
| NFR-11 | Extensibility | A new model provider, memory backend, tool provider or bus backend is added by implementing one interface and passing its conformance suite, with no core changes. |
| NFR-12 | Developer experience | `aios dev` to a running stack in ≤ 2 minutes; hello-world agent in ≤ 15 minutes; first multi-agent App in ≤ 1 day. |
| NFR-13 | Operability | Every component exposes health, metrics and structured logs; zero-downtime upgrades in HA mode; event schemas stay backward compatible within a major version. |
| NFR-14 | Cost transparency | 100% of model and tool spend is attributed to a tenant, App and run. |
| NFR-15 | Quality | ≥ 80% line coverage on kernel packages; every adapter passes its conformance suite; nightly contract tests run against live providers. |

---

## 7. Key specifications (v0.1 drafts)

These are starting points. Phase 0 turns each one into a versioned JSON Schema.

### 7.1 Event envelope

CloudEvents 1.0 attributes plus AIOS extension attributes (lowercase, alphanumeric, as CloudEvents requires):

```json
{
  "specversion": "1.0",
  "id": "01JD7X2K9Q5V8M3N4P6R7S8T9V",
  "source": "aios://acme/apps/ai-researcher/agents/research-agent",
  "type": "task.completed",
  "subject": "run/01JD7X1C3BWQ8R2N5M4K6P7S9T",
  "time": "2026-10-28T10:15:30.123Z",
  "datacontenttype": "application/json",
  "dataschema": "aios://schemas/task.completed/1",

  "tenantid": "acme",
  "appid": "ai-researcher",
  "runid": "01JD7X1C3BWQ8R2N5M4K6P7S9T",
  "correlationid": "01JD7X1C3BWQ8R2N5M4K6P7S9T",
  "causationid": "01JD7X2A4H6J8K0M2N4P6Q8R0S",
  "hopcount": 4,
  "principal": "agent:research-agent@acme",
  "onbehalfof": "user:alice@acme",
  "dataclass": "internal",
  "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",

  "data": {
    "capability": "research.web",
    "output": { "summary": "…", "sources": ["…"] }
  }
}
```

| Extension | Purpose |
|---|---|
| `tenantid`, `appid` | Isolation and routing; set by the fabric, never by agent code |
| `runid`, `correlationid` | Group every event of one run or one conversation |
| `causationid` | The event that directly caused this one; builds the DAG |
| `hopcount` | Loop and storm protection (FR-EVT-11) |
| `principal`, `onbehalfof` | Who acted, and for whom (FR-SEC-05) |
| `dataclass` | Data classification label (FR-SEC-11) |
| `traceparent` | W3C trace context (CloudEvents distributed-tracing extension) |
| `idempotencykey` | Optional; for commands that cause side effects |

Ids are ULIDs: unique and sortable by time.

### 7.2 Topic grammar and standard event catalog

- **Logical topic** (what developers see): lowercase dot-separated tokens, `<domain>.<entity…>.<verb>`. Facts end in a past-tense verb (`created`, `completed`); commands end in `requested`. The CloudEvents `type` holds the logical topic.
- **Reserved platform domains:** `app`, `run`, `step`, `task`, `agent`, `model`, `tool`, `memory`, `approval`, `human`, `policy`, `budget`, `system`. Apps use their own domains, e.g., `research.plan.created`.
- **Physical subject** (built by the fabric, never by agents): `<tenant>.<app>.<logical topic>[.<routing suffix>]`, e.g., `acme.ai-researcher.task.requested.research.web`. The fabric adds and strips this prefix, which is how tenant isolation is enforced.
- **Wildcards** (NATS-compatible): `*` matches exactly one token and `>` matches all remaining tokens. `task.requested.research.>` receives every research task. The original's `agent.research.*` topics become the capability namespace `research.*`; agents normally declare capabilities and let the kernel dispatch, and subscribe to topics directly only for side reactions.

| Domain | Standard events | Emitted by |
|---|---|---|
| `app` | `app.request.received`, `app.response.ready`, `app.installed`, `app.upgraded`, `app.uninstalled` | Access layer, control plane |
| `run` | `run.started`, `run.paused`, `run.resumed`, `run.completed`, `run.failed`, `run.cancelled` | Workflow Engine |
| `step` | `step.scheduled`, `step.started`, `step.completed`, `step.failed`, `step.retrying` | Workflow Engine |
| `task` | `task.requested`, `task.claimed`, `task.completed`, `task.failed` | Agent Kernel, agents |
| `agent` | `agent.lifecycle.<state>` (e.g., `agent.lifecycle.suspended`) | Agent Kernel |
| `model` | `model.call.completed`, `model.call.failed` | Model Kernel |
| `tool` | `tool.call.requested`, `tool.call.completed`, `tool.call.failed` | Tool OS |
| `memory` | `memory.item.written`, `memory.recalled`, `memory.item.forgotten` | Memory OS |
| `approval` | `approval.requested`, `approval.granted`, `approval.denied`, `approval.modified`, `approval.expired`, `approval.escalated`, `approval.cancelled` | Human OS |
| `human` | `human.task.assigned`, `human.task.completed` | Human OS |
| `policy` | `policy.decision.denied` (allow decisions go to the audit log only, to limit volume) | Security Kernel |
| `budget` | `budget.warning`, `budget.exceeded` | Model Kernel, Workflow Engine |
| `system` | `system.dlq.added`, `system.consumer.lagging` | Event Fabric |

### 7.3 Agent manifest

```yaml
apiVersion: aios/v1alpha1
kind: Agent
metadata:
  name: research-agent
  version: 1.4.0
  labels: { team: knowledge }
spec:
  description: Researches a question on the web and in internal sources.
  runtime:
    type: python                      # python | container | a2a | adapter:langgraph
    entrypoint: agents.research:research
  capabilities:
    - name: research.web
      input: schemas/research_task.json
      output: schemas/research_result.json
  subscriptions:                      # optional: react to facts directly (side reactions)
    - research.source.updated
  tools:
    allow: ["search.web.query", "http.page.fetch"]
  memory:
    read: [session, app, tenant]
    write: [agent]
  model:
    policy: default
    task: reasoning                   # routing hint, not a model name
  limits:
    maxSteps: 20
    maxTokens: 200000
    maxCostUSD: 1.50
    timeout: 5m
    concurrency: 10
  scaling: { min: 0, max: 20 }
  isolation: process                  # inprocess | process | container | sandbox
```

### 7.4 App manifest

```yaml
apiVersion: aios/v1alpha1
kind: App
metadata:
  name: ai-support-desk
  version: 0.3.0
spec:
  description: Tier-1 customer support. Refunds above a threshold need approval.
  entrypoints:
    - type: chat                      # REST/WebSocket; OpenAI-compatible in v1.0
      workflow: handle-ticket
    - type: event
      on: zendesk.ticket.created
      workflow: handle-ticket
  agents: [triage-agent, kb-agent, refund-agent]
  workflows: [handle-ticket]
  tools:
    - provider: zendesk               # MCP server registered by the tenant admin
    - name: stripe.refund.create
  memory:
    scopes: [session, user, app]
    knowledge:
      - source: s3://acme-support/kb/
        scope: app
  permissions:                        # shown to the tenant admin for consent at install
    tools: ["zendesk.>", "stripe.refund.create"]   # `>` = every zendesk tool (topic wildcards)
    dataClasses: [internal, confidential]
  policies: [support-desk-policy]
  modelPolicy: default
  config:
    schema: config.schema.json        # e.g., the refund approval threshold
    secrets: [ZENDESK_TOKEN, STRIPE_KEY]
  budgets:
    perRunUSD: 0.50
    monthlyUSD: 500
```

### 7.5 Agent SDK (Python)

```python
from pydantic import BaseModel
from aios import agent, Context


class ResearchTask(BaseModel):
    question: str
    depth: int = 2


class ResearchResult(BaseModel):
    summary: str
    sources: list[str]


@agent(name="research-agent", capability="research.web")
async def research(ctx: Context, task: ResearchTask) -> ResearchResult:
    background = await ctx.memory.recall(
        task.question, scopes=["app", "tenant"], budget_tokens=2_000
    )
    hits = await ctx.tools.invoke("search.web.query", q=task.question, limit=8)

    result = await ctx.model.generate(
        task="reasoning",  # routing hint resolved by the model policy
        system="You are a careful research analyst. Cite every claim.",
        messages=[
            ctx.user(f"Question: {task.question}\n\nContext:\n{background}\n\nResults:\n{hits}")
        ],
        output=ResearchResult,  # structured output, validated
    )
    await ctx.emit("research.findings.recorded", result)  # optional fact for other subscribers
    return result  # published as task.completed
```

Every `ctx.*` call is policy-checked, budgeted, traced and recorded as events (principle P1).

### 7.6 Workflow DSL (Python)

This extends the original `Start().then(...)` chain:

```python
from aios.workflow import Workflow, Step, Approval

research_report = (
    Workflow("research-report", version="1.0.0")
    .then(Step("plan", capability="planning"))  # → research.plan.created
    .then(
        Step.dynamic(
            "research",
            from_plan="plan.output.tasks",  # planner-driven fan-out
            capability="research.*",
            max_parallel=5,
        )
    )
    .then(Step("analyze", capability="analysis"))
    .then(Step("draft", capability="writing"))
    .then(
        Step("review", capability="review", on_result={"reject": "draft"}, max_loops=2)
    )  # bounded revise loop
    .then(Approval("publish-approval", approvers=["role:editor"], timeout="24h", on_timeout="deny"))
    .then(Step("publish", tool="cms.page.publish"))
    .budget(max_cost_usd=3.00, max_duration="30m")
)
```

The runtime compiles this into the graph IR and executes it as events (§4.1).

### 7.7 Memory API

```python
item_id = await memory.put(
    content="Customer prefers email over phone.",
    scope="user",
    subject="user:alice@acme",
    kind="semantic",
    retention="persistent",
    sensitivity="confidential",
    metadata={"source": "ticket-4812"},
)

hits = await memory.search(  # raw ranked retrieval
    "contact preferences",
    scopes=["user", "app"],
    k=5,
    filters={"kind": "semantic"},
    mode="hybrid",
)

pack = await memory.recall(  # deduped, ranked, cited, within budget
    task="Draft a reply to ticket 4913",
    scopes=["session", "user", "app", "tenant"],
    budget_tokens=3_000,
)

await memory.forget(subject="user:alice@acme")  # erasure across items, embeddings, summaries
```

### 7.8 Tool invocation

```python
pr = await ctx.tools.invoke(
    "github.pr.create",
    repo="acme/web",
    title="Fix login timeout",
    head="fix/login",
    base="main",
    idempotency_key=ctx.step_key("create-pr"),
)
```

The runtime resolves `github.pr.create` to the provider bound for this tenant and environment (native connector, MCP server or OpenAPI), then runs the pipeline in FR-TOOL-05.

### 7.9 Policy

AIOS policies are authored in YAML (the original example, made precise) and compiled to the policy engine:

```yaml
apiVersion: aios/v1alpha1
kind: Policy
metadata:
  name: research-agent-policy
  version: 1.0.0
spec:
  subject: { agent: research-agent }
  allow:
    - action: tool.invoke
      resource: "search.>"            # tool patterns use the topic wildcards (§7.2)
    - action: memory.read
      scopes: [session, app, tenant]
  deny:
    - action: tool.invoke
      resource: "filesystem.*.delete"
  requireApproval:
    - action: tool.invoke
      when: { sideEffect: [external_comm, financial] }
      approvers: ["role:team-lead"]
```

Compiled to Cedar (ADR-007) by `aios.security.cedar`; excerpt. Tool patterns become checks on
the name's tokens, so `*` keeps matching exactly one token, and `requireApproval` becomes a
`forbid` that an approval lifts:

```cedar
permit (principal == Agent::"research-agent", action == Action::"tool.invoke", resource)
when { resource.provider == "search" };

forbid (principal == Agent::"research-agent", action == Action::"tool.invoke", resource)
when { resource.provider == "filesystem" && resource.verb == "delete" };

forbid (principal == Agent::"research-agent", action == Action::"tool.invoke", resource)
when { ["external_comm", "financial"].contains(resource.sideEffect) }
unless { context.approved };
```

### 7.10 Model routing policy

```yaml
apiVersion: aios/v1alpha1
kind: ModelPolicy
metadata: { name: default, version: 1.0.0 }
spec:
  rules:
    - match: { dataClass: restricted }
      require: { deployment: private }        # never leaves the tenant's infrastructure
      candidates: [private/<open-weights-model>]
    - match: { task: coding }
      candidates: [anthropic/claude-sonnet-5-5, openai/<coding-model>]
    - match: { task: reasoning }
      candidates: [anthropic/claude-opus-5-5, openai/<reasoning-model>]
    - match: { task: classification }
      candidates: [anthropic/claude-haiku-5-5, private/<small-model>]
  default:
    candidates: [anthropic/claude-sonnet-5-5, google/<general-model>]
  fallback: next-candidate                    # on error, timeout or rate limit
  retries: { max: 2, backoff: exponential }
  budgets: { perRunUSD: 2.00, perTenantMonthlyUSD: 5000, onSoftLimit: downgrade }
```

Candidate lists are revisited whenever eval results change. `<…>` placeholders are filled from the model registry at deployment.

---

## 8. Technology stack (reviewed)

| Layer | Original | Recommendation | Rationale |
|---|---|---|---|
| Language | Python | Python 3.12+ for runtime and SDK; TypeScript for console and client SDK | The AI and MCP ecosystems live in Python, and the workload is I/O-bound (waiting on models and tools). Consider Go or Rust only for a hot path that measurements prove. |
| API | FastAPI | FastAPI + Pydantic v2; SSE/WebSocket streaming; OpenAPI 3.1 | Typed models also generate the JSON Schemas for manifests and events. |
| Async runtime | AsyncIO | asyncio (uvloop in production) | — |
| Messaging (MVP) | PostgreSQL LISTEN/NOTIFY | PostgreSQL **event table + transactional outbox**, with NOTIFY used only as a wake-up hint | §8.1 |
| Messaging (v1.0) | NATS | NATS JetStream | Durable streams, consumer groups, wildcard subjects that match the AIOS topic grammar, a KV store, and light operations. |
| Messaging (later) | Kafka | Kafka/Redpanda adapter only when a customer requires it | Many enterprises already run Kafka; don't make it a dependency. |
| Persistence | PostgreSQL, later CockroachDB | PostgreSQL 17+ with partitioning and row-level security; HA via CloudNativePG or managed Postgres | Postgres goes a long way with partitioning and replicas. Evaluate distributed SQL only once multi-region active-active writes are a real requirement; it also changes the LISTEN/NOTIFY and pgvector assumptions. |
| Vector memory | Qdrant | **pgvector first**; Qdrant adapter in v1.0; others via adapters | Keeps the MVP on one database. |
| Workflow engine | (custom) | Custom event-sourced graph engine; optional Temporal adapter later | §5.4 design note |
| Policy engine | — | **Cedar** (embeddable, formally analyzable), decided in ADR-007; OPA/Rego stays possible behind the same port | Don't invent a policy language; compile AIOS YAML to an engine. |
| Model access | — | Thin internal interface over official provider SDKs; evaluate LiteLLM as an underlying adapter | Owning the interface keeps accounting, policy and normalization in AIOS's control. |
| Tool protocols | MCP | Official MCP Python SDK; OpenAPI import; A2A SDK | Standards |
| Tracing | OpenTelemetry | OTel SDK + Collector; Jaeger or Grafana Tempo locally; GenAI semantic conventions pinned | — |
| Identity | — | OIDC (Keycloak for local and self-hosted; any IdP in production) | Never build your own authentication. |
| Secrets | — | HashiCorp Vault / OpenBao; cloud secret managers | — |
| Sandboxing | — | gVisor or Firecracker micro-VMs; Kubernetes Agent Sandbox on Kubernetes | Needed for code-executing agents (AI Developer). |
| UI | React, TypeScript, Next.js | Same, plus React Flow for the DAG explorer and workflow designer | — |
| Packaging and deployment | — | uv workspaces, Docker images, Docker Compose, Helm, Kubernetes operator (v1.0) | — |
| Testing | — | pytest, testcontainers, Hypothesis, recorded model cassettes, Toxiproxy for chaos tests | — |

### 8.1 PostgreSQL as the MVP event fabric, done safely

`LISTEN/NOTIFY` alone is not a message bus. Notifications are lost when no session is listening, payloads are capped at 8,000 bytes, and heavy NOTIFY traffic serializes commits through a global lock. The MVP design is:

1. **Events are rows.** Events are inserted into an `events` table in the same transaction as the state change that produced them (transactional outbox, FR-EVT-07).
2. **NOTIFY only wakes consumers.** It carries no payload, and consumers also poll at a short interval in case a notification is missed.
3. **Safe ordering.** Events are ordered by `(transaction id, position)`, and readers only consume rows from transactions older than the current snapshot's `xmin`. This avoids the classic bug where `bigserial`-ordered event tables silently skip events from transactions that commit out of order.
4. **Competing consumers** claim deliveries with `SELECT … FOR UPDATE SKIP LOCKED` and leases. **Broadcast subscribers** track offsets in a table.
5. **Monthly partitions** with archival to object storage keep the table fast.

Because the same `EventBus` port and conformance suite apply to every backend, moving to NATS JetStream later is a configuration change for apps.

---

## 9. Product strategy

The five products in the original are five packagings of **one runtime**. Build the runtime once and release them in this order:

| Order | Original product(s) | What it becomes | When |
|---|---|---|---|
| 1 | **Product 1 (AI Event Bus) + Product 3 (Agent Kubernetes)** → **AIOS Core** | Open-source runtime: Event Fabric, Agent Kernel, Workflow Engine, Model Kernel, Tool OS, CLI and SDK. Drives developer adoption. | MVP (M2) → Developer Preview (M3); Kubernetes operator at v1.0 (M4) |
| 2 | **Product 2: AI Workflow Cloud** | Workflow Engine + Trace Explorer + replay as a managed service; visual designer later. | After M3; designer in Phase 5 |
| 3 | **Product 4: AI Enterprise Runtime** | Commercial edition: SSO/SCIM, ABAC, audit export, data residency, private LLM routing, HA and support. **The revenue engine.** | Pilots from M4; GA in Phase 5 |
| 4 | **Product 5: Universal AI Runtime** | The full vision. It emerges from 1–3 plus A2A/MCP interop and the App registry. | Ongoing |

**Why not sell the event bus alone:** brokers are commodities. "Kafka for agents" can't be defended without the agent semantics on top (capabilities, budgets, replay, policy).

**Reference apps** (each one proves a phase):

| App | Proves | Milestone |
|---|---|---|
| AI Researcher | Planning, parallel fan-out, tools, model routing, durability (read-only, so low risk) | M1 |
| AI Support Desk | Memory, write tools, approvals, injection defenses, multi-tenancy | M2 |
| AI Developer | Sandboxed code execution, GitHub tools, A2A | M4 |
| AI Accountant, AI Architect | Built with design partners on the finished platform | Phase 5 |

---

## 10. Success metrics

| Area | Metric | Target |
|---|---|---|
| Adoption | Time from install to the first running agent | ≤ 15 minutes |
| Adoption | Active design partners | 5 by M3; 2 paid pilots by M4 |
| Reliability | Run success rate for reference apps, excluding model-quality failures | ≥ 99% |
| Reliability | Events lost in chaos tests | 0 |
| Cost | Cost per successful run vs. a single-premium-model baseline (G6) | Measure at M1, then set the target |
| Safety | High-risk actions executed without a required approval | 0 |
| Safety | Known injection cases in the red-team suite that are blocked or escalated | 100% |
| Operations | Median approval turnaround | Tracked; target agreed with design partners |
| Quality | Eval regression allowed before the deploy gate blocks | Configurable; default 0 |

---

## 11. Assumptions, open questions and decisions needed

**Assumptions**

| # | Assumption |
|---|---|
| A1 | The team is Python-first; the UI is TypeScript. |
| A2 | Self-hosted and on-prem deployment are must-haves for target buyers; managed SaaS comes later. |
| A3 | The first target segment is regulated mid-size to large enterprises (finance, healthcare), confirmed through design-partner interviews. |
| A4 | A core team of about four engineers; plan durations assume this. |
| A5 | Docs and UI are English-first. |
| A6 | One region per installation for the MVP. |

**Open questions for the project owner** (decide in Phase 0; a recommended default is given for each, and decisions are marked):

| # | Question | Recommended default |
|---|---|---|
| Q1 | License and business model | **Decided 29 Oct 2026 (ADR-012):** open core, Apache-2.0 core, commercial enterprise edition |
| Q2 | First wedge and first paying segment | AIOS Core for developers; Enterprise Runtime pilots with regulated organizations |
| Q3 | Self-hosted or managed SaaS first | Self-hosted first; SaaS after M4 |
| Q4 | Policy engine | **Decided 29 Oct 2026 (ADR-007):** Cedar |
| Q5 | Own workflow engine or Temporal | Own graph engine; Temporal adapter at P2 |
| Q6 | Second reference app | AI Support Desk |
| Q7 | Product name | Decide after a trademark check (see §1.6) |
| Q8 | Team size and budget | The plan assumes four engineers; adjust the timeline if that differs |

---

## 12. Sources consulted (October 2026)

Mostly secondary sources; check the primary specifications before relying on any detail.

- MCP and A2A under the Linux Foundation's Agentic AI Foundation: [FlowHunt](https://www.flowhunt.io/blog/agentic-ai-foundation-a2a-mcp-standards/), [State of agentic AI standards 2026](https://datalakehousehub.com/blog/state-of-agentic-ai-standards-2026)
- OpenTelemetry GenAI semantic conventions still in Development status: [Dash0](https://www.dash0.com/knowledge/opentelemetry-genai-semantic-conventions-explained), [DEV Community](https://dev.to/azena-ai/opentelemetrys-genai-semantic-conventions-are-not-stable-yet-heres-what-actually-shipped-in-2026-3mke)
- Cloud-native agent runtimes: [kagent (CNCF)](https://www.cncf.io/blog/2025/08/19/celebrating-100-days-of-kagent/), [Kubernetes Agent Sandbox](https://agentic-ai.readthedocs.io/en/latest/Standards/k8s-agent-sandbox/), [Dapr Agents v1.0](https://businesstechnavigator.com/news/dapr-agents-v1-production-kubernetes-2026), [CNCF evaluates MCP](https://forkast.news/cncf-evaluates-mcp-as-the-cloud-native-agent-wire-spec-and-the-standardization-arc-just-reached-infrastructure/)
- Durable execution for agents: [Temporal funding](https://forkast.news/temporal-raises-550m-at-12-55b-valuation-signaling-durable-execution-as-core-agent-infrastructure/), [Temporal tradeoffs](https://rubinlake.com/en/technology-radar/ai-and-data-engineering/temporal-durable-agents)
- Agent memory: [Letta forum comparison](https://forum.letta.com/t/agent-memory-solutions-letta-vs-mem0-vs-zep-vs-cognee/85), [Mem0 alternatives](https://vectorize.io/articles/mem0-alternatives)
- LLM gateways: [LLM gateway comparison 2026](https://wavect.io/blog/llm-gateway-router-comparison-2026.md), [OpenRouter alternatives](https://pinggy.io/blog/best_ai_llm_routers_openrouter_alternatives/)

# ADR-014: Agent code runs outside the platform's process, and generated code runs in a sandbox

- **Status:** Proposed — drafted 7 Nov 2026 for the owner to decide, after the
  [Phase 3 review](../reviews/phase-3.md) found it overdue (finding 4). Nothing is built yet.
- **Date:** 2026-11-07
- **Deciders:** Project owner, tech lead, platform engineer, security engineer
- **Requirements:** FR-AGT-02, 04, 05, 06, 09, 10, 13; FR-APP-04; FR-SEC-02; NFR-03, NFR-10;
  WP 4.2, WP 4.4; M4 exit 4; ADR-011, ADR-015, ADR-017, ADR-021

## Context

Every agent runs in the worker's process. `load_entrypoint` imports the handler, and the worker
calls it with a `Context` that holds the bus, the model, tool and memory gateways, the budget, the
call sites and the run's taint. When a worker starts an App version, `_import_from` puts the
version's code folder first on `sys.path` and forgets the App's modules, because Python imports a
module once per process (ADR-015).

The manifest already has `spec.isolation`, with `inprocess`, `process` (the default), `container`
and `sandbox`, and the AI Support Desk asks for `process`. Nothing reads the field: every agent runs
in the worker whatever it says.

Four open items wait on this decision (Phase 3 review, finding 4):

- **Agent code pinned per App version (FR-APP-04).** One process holds one version of a module, so
  after an upgrade, in-flight runs' remaining tasks run the new code.
- **Forking with an older agent version (ADR-021).** A fork runs the code installed now.
- **A hostile handler ([threat model](../threat-model.md), "What isn't covered").** Taint stops
  mistakes and fooled models. Code in the process can do far more:
  - set `ctx._tainted`;
  - publish `approval.granted` through `ctx._bus`, past `ctx.emit`'s check on platform domains;
  - reach the database pool;
  - read provider keys from the environment;
  - touch other tenants' agents in the same process.

  That is why only a `platform-admin` can install an App (ADR-011).
- **Untrusted code execution (threat model).** There is no sandbox. A `code_exec` tool needs a
  person once the run is tainted (II-09), and that is all.

Two more follow from in-process hosting. A handler that blocks the event loop, or crashes in native
code, stalls or kills every agent on that worker, which FR-AGT-04 forbids. And there is no way to
host a container agent (FR-AGT-02, WP 4.2).

The two problems differ in kind. **Agent code** was installed by an admin from a digest someone
reviewed (ADR-015), so the threat is a bad or compromised App reaching the platform and other
tenants. **Code a model wrote** is hostile by assumption, every time. They get different answers.

## Decision

**1. The platform keeps `Context`; only the handler moves.** The worker becomes the *agent host*.
It keeps everything `Context` holds today. Agent code runs in a *runner*: a separate process that
imports the handler and gives it a `ctx` with the same API. Each `ctx` call becomes a request to
the host, which runs the same `Context` method it runs now and returns the result:

- `ctx.model.generate`, `complete` and `embed`;
- `ctx.tools.invoke`, `describe` and `call`;
- `ctx.memory.*`;
- `ctx.emit`, `ctx.taint` and `ctx.log`.

Pure helpers (`step_key`, `quote`, `user`, `assistant`) run in the runner. The protocol is
JSON-RPC 2.0. It is newline-delimited over stdin and stdout for a runner on the same host, as
MCP's stdio transport is. For containers and sandboxes, the same messages travel over a stream the
runner opens to the host, with mutual TLS and the agent's SPIFFE id (ADR-011). That is where
issued identities arrive (FR-SEC-02, P1).

**2. The host computes taint, policy and identity; the runner only asks.**

- **Taint and labels.** `Context.admit` runs in the host on every tool output and memory read it
  hands over, so taint and the data label are folded where agent code can't reach them
  (ADR-017). The runner can read `ctx.tainted` and add taint with `ctx.taint(source)`. Nothing it
  sends can clear either.
- **Identity.** The host builds each `CallSite` (agent, principal, tenant, step, taint, label) from
  the task it handed over. The runner's messages carry an opaque handle for a live task that
  belongs to that runner, and nothing else about who is asking. A runner can't act for another
  task, agent or tenant, or for a task that has finished.
- **Policy and limits.** Cedar decisions, manifest caps (`tools.allow`), egress lists, approvals
  and budgets stay in Tool OS, the Model Kernel, Memory OS and `Budget`, as now. The kill switch
  is checked before a task is handed over, and a suspension refuses the agent's further calls.
- **Credentials.** The runner holds none: no database, no bus, no model keys, no secrets, and its
  environment is cleared. Tool credentials stay with Tool OS (FR-TOOL-06). Agent code needs no
  network either, because models, tools and memory all go through `ctx`. So a Linux runner gets a
  network namespace with no interfaces.

**3. A runner serves one App version in one tenant, at one isolation level.** It is started from
that version's digest folder (ADR-015). It stays warm while the version has tasks or in-flight
runs, and it stops when idle (scale to zero, FR-AGT-06). Concurrency inside a runner follows
`limits.concurrency`.

- `run.started` records the App's digest, and every `task.requested` carries it. The host sends
  each task to that version's runner, so in-flight runs finish on the code they started with
  (FR-APP-04).
- A fork can name an older version (ADR-021). Bundles are kept in the database (ADR-019), so any
  installed version can be started.
- Agents of one App version share a runner. They are one reviewed bundle by one author, and
  per-agent principals still hold for policy and audit, because the host binds each task to its
  agent. Two tenants never share a runner, so module state can't leak between them.
- A runner that crashes fails its tasks with `task.failed` (`agent.crashed`), and the supervisor
  starts it again with backoff (FR-AGT-04).

**4. Isolation levels mean what they say, and they decide who may install.**

| Level | Where agent code runs | Boundary | Who may install | Hosts |
|---|---|---|---|---|
| `inprocess` | The worker's process, as today | None | `platform-admin` | Any. Also `aios dev`, for hot reload (FR-AGT-13) |
| `process` (default) | A child process of the worker | A process with no credentials. On Linux also its own user id, no network, resource limits and a seccomp profile, on a kernel it shares with the platform | `platform-admin` | Any. Windows and macOS get version and crash isolation, not a security boundary |
| `container` | A container or pod (WP 4.2): the AIOS runner image with the bundle, or the App's own `runtime.image` | Namespaces, cgroups and seccomp; shares the node's kernel | `platform-admin` | Linux nodes |
| `sandbox` | A container under a sandboxed runtime: gVisor by default, Kata Containers where nodes allow VMs | A user-space kernel, or a VM | `tenant-admin` | Linux nodes |

A **tenant-admin may install an App whose agents all run at `sandbox`**. Anything weaker still needs
a `platform-admin`, because a kernel escape from a shared kernel reaches a host that holds
platform credentials. This changes ADR-011's rule, and it is a decision for the owner.

A worker serves an agent only at its declared level or a stronger one. Placement uses labels
(FR-AGT-05): a worker that can't give `sandbox` doesn't take those tasks, and the run says what it
is waiting for, as it does for a suspended agent. The default stays `process`.

**5. Generated code runs in a sandbox, through a tool, never in a runner.** Code execution is a
Tool OS tool with side effect `code_exec` (for example `code.python.run` and
`shell.command.run`), backed by a `Sandbox` port with one conformance and escape suite (point 7).
Its output is untrusted, so it taints the run, and a tainted run needs a person to run code
(II-09).

- **Limits.** Each call has CPU, memory, process-count, disk and wall-time limits. The network is
  off by default. When the App's egress list allows hosts, traffic goes through an egress proxy
  that applies the same lists as Tool OS (ADR-017).
- **Nothing inside.** No credentials ever enter the sandbox. Files go in and out through the
  tool's arguments and outputs, as a size-capped workspace archive. AI Developer opens its pull
  request through a separate GitHub tool, with an approval, outside the sandbox.
- **Never shared.** A sandbox belongs to one run in one tenant, and it is destroyed when the run
  ends. Warm pools hide the start time.

**6. Sandbox technology, by deployment:**

- **Kubernetes: Kubernetes Agent Sandbox.** It is a SIG Apps project (v1.0.10, 6 Nov 2026) that
  gives each sandbox a pod with stable identity, and it delegates isolation to the pod's
  RuntimeClass. AIOS creates a `SandboxClaim` from a `SandboxTemplate`, and a `SandboxWarmPool`
  keeps pods ready. It has a Python client. The template uses gVisor by default. Kata Containers
  is the option where nodes can run VMs and an operator wants a VM boundary.
- **One Linux host (Docker Compose): gVisor's `runsc`** as a Docker runtime. It needs Linux
  4.14.77 or later on x86_64 or ARM64, and gVisor ships a release roughly every few weeks
  (20261102.0 is the latest).
- **Firecracker: not now.** It gives the strongest boundary of the three, a microVM per sandbox,
  but it needs KVM on Linux, which usually means bare metal or nested virtualization. It also
  needs a jailer in production, plus a guest kernel and root-filesystem pipeline that AIOS would
  build and patch itself. Revisit it for a managed service (Phase 5), where AIOS runs the hosts.
  Until then a VM boundary on Kubernetes comes through Kata, with no AIOS code.
- **Windows and macOS laptops: no local sandbox.** The code tool is off unless a remote sandbox (a
  cluster's Agent Sandbox) is configured. A `docker-unsafe` adapter runs plain containers for
  tests. It refuses to start outside development mode and says it isn't a security boundary.
  NFR-10 holds for everything except running generated code locally.

**7. One escape suite for every sandbox adapter** (WP 4.4, M4 exit 4), in `tests/security/sandbox/`.
Code inside the sandbox must fail to:

- read the host's files or another sandbox's;
- reach the cloud metadata address, the AIOS API, the database, NATS or the internet;
- exhaust memory, processes or disk on the host (fork bomb, filling `/tmp`);
- outlive its time limit;
- find a credential in its environment;
- see a file a previous run left behind.

The `docker-unsafe` adapter is expected to fail some of these, and the suite says which.

**8. Hostile-handler cases join the red-team corpus.** Each runs at `process` and must get nothing:

- a handler that tries to clear taint or forge a principal;
- one that publishes `approval.granted`;
- one that reads keys from its environment or opens a socket to the database;
- one that calls `ctx` with another task's handle;
- one that reads module state left by another tenant.

When they pass, the threat model's "hostile handler" row moves from the gaps to the controls.

## Alternatives considered

**Where agent code runs:**

| Option | Trade-off | Why not chosen |
|---|---|---|
| **Stay in process, with audit hooks or a restricted Python** | No new moving parts | Python can't sandbox itself: PEP 578 says audit hooks aren't a sandbox, and every attempt at a restricted CPython has been broken. It fixes none of the four items |
| **Subinterpreters (PEP 734, `concurrent.interpreters` in Python 3.14)** | Several versions of an App's modules in one process, cheaply | Not a security boundary: one process, one address space. Python's docs say not all PyPI packages support multiple interpreters yet. It could solve pinning alone; the runner solves pinning and the rest |
| **Give the runner the whole `Context`**, with a bus connection and scoped credentials | No round trip per `ctx` call | Taint, budget and call sites would be computed where agent code can change them, which is the hostile-handler gap again, and the runner would hold credentials |
| **gRPC** instead of JSON-RPC | Typed contracts, mature streaming | A protobuf toolchain for a dozen methods. The envelope and payloads are JSON already, and MCP's stdio framing is a known shape |
| **A container image per App**, always | One artifact per version | Most agents need only Python and their bundle. The standard runner image plus the bundle covers them, and `runtime.image` remains for those that need more (ADR-015's open alternative) |
| **A runner per agent** rather than per App version | Per-agent memory isolation | Several times the processes. Agents of one bundle share an author and a review, and the host already binds each task to its agent |
| **WebAssembly for agent code** | Strong isolation on every OS | Python agents need native wheels (`pydantic-core`, framework SDKs), which WebAssembly runtimes don't load generally |

**What runs generated code:**

| Option | Strength | Cost | Verdict |
|---|---|---|---|
| **gVisor (`runsc`)** | A user-space kernel between the code and the host. An OCI runtime, so Docker and Kubernetes use it directly. The systrap platform needs no virtualization | Some system calls are slower or missing, which matters for I/O-heavy test suites. Linux hosts only | **Default** on one host and on Kubernetes |
| **Firecracker** | A microVM per sandbox, a minimal device model, fast start | Needs KVM, so bare metal or nested virtualization. AIOS would own the jailer, guest kernel, root filesystem and VM networking. It has security advisories of its own (GHSA-48ww-rqpp-2mgm, 4 Nov 2026), as any VMM does | Later, for a managed service |
| **Kubernetes Agent Sandbox** | Orchestration made for this: sandbox pods, claims, templates and warm pools. Isolation comes from the RuntimeClass (gVisor, Kata). A Python client | Kubernetes only (1.25 or later; 1.29 recommended). A young project: its CRDs are `v1beta1` | **Use** on Kubernetes, with gVisor |
| **Kata Containers** under Agent Sandbox | A VM per pod, behind the same API | Nodes that can run VMs; more memory per sandbox | Option for operators who want a VM boundary |
| **Plain containers (runc)** | Everywhere Docker is | They share the host kernel, so they aren't a boundary for hostile code | Tests only, as `docker-unsafe` |
| **WebAssembly (Pyodide, wasmtime)** | Strong, portable, fast start | No native extensions, no subprocesses, no real toolchains. AI Developer needs `pytest`, `git` and compilers | Not for this tool |
| **A hosted sandbox service** | Nothing to run | A mandatory proprietary service breaks NFR-10, and code and data leave the installation | Could be an optional `Sandbox` adapter, never the default |

## Consequences

- **Built, it closes the four open items.** Versions are pinned and forks can use older ones. A
  hostile handler is contained by a process boundary, and by a sandbox for tenant-installed Apps.
  Generated code runs in gVisor or a VM. Crashes and blocking handlers stay inside their runner
  (FR-AGT-04). Three of the four come with the `process` runner, at the start of WP 4.2. The
  fourth comes with the sandbox tool in WP 4.4.
- **Costs.** Each `ctx` call becomes a local round trip, well under a millisecond to a few
  milliseconds, next to model calls that take seconds. NFR-03's 5% is measured, not assumed.
  Payloads are serialized once more. A runner costs a Python process (tens of megabytes) per
  tenant and App version while warm, which is why idle runners stop. A cold start costs the
  App's imports, so the first task after idling is slower.
- **What agents must change.** Anything passed through `ctx` must be JSON, which `ctx` calls
  almost entirely are already. An agent that opens its own network connections stops working at
  `process` on Linux, and must move them into a tool, which is where egress control belongs. The
  LangGraph and OpenAI Agents SDK adapters run inside the runner and call the Model Kernel and
  Tool OS through `ctx`, as now.
- **Roles change.** Tenant admins can install sandboxed Apps. ADR-011 and ADR-015 say installing
  needs `platform-admin` because code shares a process, and they would point here.
- **Laptops.** `aios dev` keeps `inprocess` and hot reload, and `process` works on every OS.
  Generated code can't run locally on Windows or macOS, so the AI Developer demo needs Linux or a
  cluster.
- **A side benefit.** The runner protocol is language-neutral, so agents in other languages, and
  container agents (FR-AGT-02), use the same boundary.
- **Docs to update when built.** The threat model's untrusted-code and hostile-handler rows, the
  security review, and the manifest reference's note on `spec.isolation`.

## Implementation

Nothing is built. If accepted:

**First, and small:** until runners exist, installing an agent whose `spec.isolation` isn't
`inprocess` logs that the level isn't provided yet, and the manifest reference says so, so the
field stops claiming what doesn't happen.

**WP 4.2:**

- **`src/aios/kernel/runner.py` (new, host side):** a `Runner` interface with `InProcessRunner`
  (today's `definition.handler(ctx, payload)`) and `ProcessRunner` (start, supervise, JSON-RPC,
  task handles, idle stop). `ContainerRunner` and `SandboxRunner` follow.
- **`src/aios/runner/` (new, `python -m aios.runner`):** imports the entrypoint from the digest
  folder and gives the handler a `RemoteContext` with `Context`'s public API. An import-linter
  contract keeps it to `aios.core` and `aios.sdk`: no `fabric`, `security` or stores.
- **`src/aios/kernel/worker.py`:** `_run_task` and `_run_subscription` hand the call to the runner
  for the agent's level. `src/aios/kernel/context.py` gains a dispatcher from protocol methods to
  `Context` methods. A test checks that `RemoteContext` and `Context` expose the same public names.
- **`src/aios/kernel/definitions.py`:** definitions carry `isolation` and the entrypoint string,
  and only `inprocess` imports the handler. `_import_from` in `src/aios/controlplane/bundle.py` is
  then used only for `inprocess`.
- **`src/aios/core/catalog.py`:** the App's digest goes on `app.request.received`, `run.started`
  and `task.requested`, and ADR-021's `source` can name a version.
- **The installer:** checks isolation level against the installer's role.
- **Tests:** `tests/e2e/` runs the reference Apps at `inprocess` and `process`, and
  `tests/redteam/corpus.yaml` gains the hostile-handler cases.

**WP 4.4:**

- **`Sandbox` port:** in `src/aios/core/ports.py`, with adapters in `src/aios/tools/sandbox/`
  (`agent_sandbox`, `gvisor`, `docker_unsafe`).
- **The code tools:** in `src/aios/tools/builtins.py`.
- **The escape suite:** in `tests/security/sandbox/`, run nightly against a gVisor host and a kind
  cluster with Agent Sandbox.
- **Deployment:** a `runsc` runtime in the compose stack's sandbox profile, and the Agent Sandbox
  install in the Helm chart (WP 4.3).

## Sources

Checked on 7 Nov 2026.

- **gVisor:**
  - [releases](https://github.com/google/gvisor/releases): 20261102.0, 2 Nov 2026.
  - [docs](https://gvisor.dev/docs/): `runsc` is an OCI runtime.
  - [platforms](https://gvisor.dev/docs/architecture_guide/platforms/): systrap has been the
    default since mid-2023, KVM runs best on bare metal, and ptrace is no longer supported.
  - [install](https://gvisor.dev/docs/user_guide/install): x86_64 and ARM64, Linux 4.14.77 or
    later.
- **Firecracker:**
  - [releases](https://github.com/firecracker-microvm/firecracker/releases): v1.17.0, 10 Sep 2026.
    CVE-2026-5747 was fixed in v1.14.4 and v1.15.1.
  - [getting started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md):
    KVM, x86_64 and aarch64 Linux, `/dev/kvm`.
  - [README](https://github.com/firecracker-microvm/firecracker/blob/main/README.md): the jailer
    for production.
  - [advisories](https://github.com/firecracker-microvm/firecracker/security/advisories):
    GHSA-48ww-rqpp-2mgm, 4 Nov 2026.
- **Kubernetes Agent Sandbox:**
  - [releases](https://github.com/kubernetes-sigs/agent-sandbox/releases): v1.0.10, 6 Nov 2026.
  - [repository](https://github.com/kubernetes-sigs/agent-sandbox): SIG Apps; isolation delegated
    to runtimes such as gVisor and Kata through RuntimeClass; `pip install k8s-agent-sandbox`;
    Kubernetes 1.25 or later, 1.29 recommended.
  - [API](https://agent-sandbox.sigs.k8s.io/docs/api/): `Sandbox` is `agents.x-k8s.io/v1beta1`,
    and `SandboxClaim`, `SandboxTemplate` and `SandboxWarmPool` are
    `extensions.agents.x-k8s.io/v1beta1`.
- **Python:**
  - [PEP 734](https://peps.python.org/pep-0734/) is Final, shipped as
    [`concurrent.interpreters`](https://docs.python.org/3/library/concurrent.interpreters.html)
    in 3.14.
  - [PEP 578](https://peps.python.org/pep-0578/): audit hooks aren't a sandbox.

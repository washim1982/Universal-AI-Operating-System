# Phase 3 review: M3 "Developer Preview"

| | |
|---|---|
| **Date** | 7 November 2026 |
| **Covers** | WP 3.1 to WP 3.7 ([implementation plan](../../implementation-plan.md), Phase 3) |
| **By** | The team that built it. This is a self-review, for the project owner to sign off |
| **Asks** | Exit criteria checked, risk register and roadmap re-estimated (plan §14) |

## Verdict

**M3 is not closed yet.** All seven work packages are built. Exit criteria 2 to 6 pass in
automated tests, two of them through tests this review added. Exit 1 needs three outside
developers, which only the owner can arrange. Two things block sign-off even for the criteria
that pass:

- CI on GitHub has failed its PostgreSQL job on every push since 30 October. The cause is found and
  fixed in the working tree (finding 1), but CI stays red until that is pushed.
- Everything since commit `e743f97`, including WP 3.6, WP 3.7 and this review, is only in the
  working tree.

The review also found and fixed a gap in tracing. Since it was written, the threat model has been
brought up to date with Phase 3, and ADR-013 and ADR-014 have been drafted for the owner.

## Exit criteria

| # | Criterion | Standing | Evidence |
|---|---|---|---|
| 1 | Three design-partner developers each build and install a new App from a template, using only the docs, within one day | **Not met**: it needs people | What can be shown without them: six templates, each with a test that passes as made (`tests/unit/cli/test_templates.py`), and the quickstart run as written (testing guide G6). The design-partner interview script and target list (Sprint 1 backlog) are still open |
| 2 | A seeded bug in a reference app is found with the Trace Explorer, and the fix is confirmed by forking the failed run from the faulty step | **Met** (test added by this review) | `test_a_seeded_bug_is_found_in_the_trace_and_its_fix_confirmed_by_a_fork`: a bug ships in the AI Researcher's writer. The run completes, and the eval gate blocks it (`report-shape fell`). The trace shows `draft` was given four sources and returned one. The fix ships, and the run is forked from `draft`: `plan`, `research` and `analyze` are copied, the report cites all four, and the gate passes. It runs on recorded answers, and it reads the Trace Explorer's data through its API |
| 3 | The CI eval gate blocks a prompt change that lowers the Researcher's eval score | **Met on recordings** | `test_a_change_that_scores_lower_is_blocked`, and the CI step `aios eval run ... --baseline`. The recording predates five of today's prompts, so the suite warns about drift (`onDrift: warn`) instead of failing on it. Until it is recorded again with a real model, the gate can't vouch for today's prompts |
| 4 | Installing an App shows the consent step, and denying a permission blocks that tool at runtime | **Met** | `tests/unit/controlplane/test_installer.py` (both halves of M3 exit 4), testing guide Part 16 |
| 5 | An approval is completed from Slack with a signed action link | **Met against a stand-in Slack** | `tests/unit/human/test_channels.py` (the M3 exit 5 section). Approving from a real Slack workspace is manual step Y8 of the testing guide, and nobody has run it yet |
| 6 | A LangGraph agent runs as an AIOS agent with full tracing and policy enforcement | **Met** (tests added by this review) | `test_a_langgraph_agent_is_traced_and_waits_for_a_person`: a ReAct graph that turns every tool error into a message still pauses for a lead's approval. The refund runs once after the yes, and every span sits under the run's own. `test_a_langgraph_agent_cant_use_a_tool_policy_denies`: a denied tool never runs. Also `tests/unit/adapters/test_langgraph.py`. These tests found finding 2 |

## What was checked, fresh, on 7 November

| Check | Result |
|---|---|
| Full suite on PostgreSQL (Windows, Python 3.14) | 2,521 passed, none failed, in 6 min 5 s |
| Line coverage (NFR-15: at least 80% on kernel packages) | 95.3% overall. `fabric` 96.4, `kernel` 93.4, `workflow` 96.3, `models` 97.6, `tools` 94.4, `memory` 97.5, `security` 98.9, `human` 97.2, `observability` 95.5, `controlplane` 96.8 |
| ruff, ruff format, import layers, pyright (strict) | Clean |
| `aios specs check`; `aios specs compat` against `e743f97` | Pass; compatible |
| `aios docs reference --check`; `mkdocs build --strict` | Pass |
| Red-team corpus | 46 passed |
| AI Researcher eval gate, as CI runs it | Passed at 0.829, with 5 drifted calls |
| Console: vitest, typecheck, ESLint | 185 passed; clean |
| TypeScript client: vitest, typecheck | 9 passed; clean |
| CI's PostgreSQL job, run here at `e743f97` with Python 3.12 against a trust-auth `pgvector:0.8.0-pg17`, as on GitHub | 404 passed |
| Deliberate breaks of this review's tracing fix | 3 of 3 caught |
| GitHub's logs of the two failed PostgreSQL jobs | Both failed only the kill test (finding 1) |
| The kill scenario, 40 times on PostgreSQL, before the fix | 3 crashes repeated a call; none of those calls had been stored |
| The fixed kill test, 50 times on PostgreSQL | All passed, including a crash that repeated a call |
| The same, with reuse of stored calls turned off | 7 of 10 failed |
| CI's PostgreSQL job command, after the fix (Windows, Python 3.14) | 425 passed |
| Red-team corpus, after the threat-model update (finding 3) | 52 passed |

## Findings

### 1. CI is red on GitHub (cause found and fixed; push to confirm)

GitHub's PostgreSQL job ("Conformance, isolation and acceptance tests on PostgreSQL") failed for
`c366d92` (30 Oct) and `e743f97` (6 Nov). The last green run was `7c0d8d9`. Every other job
passed, including the full suite on Ubuntu, Windows and macOS. M1's status in the plan says it was
"pending the first CI run of the PostgreSQL job". That run has happened, and it failed.

Both logs show one failure: the kill test, `test_survives_a_worker_crash_without_repeating_work`
(M1 exit 2). After the worker was killed mid-research, one research model call was made a second
time: `research#1` on 30 Oct, `research#2` on 6 Nov.

A task that runs again reuses every model call already stored for it. So a call is repeated only
when the worker dies after the model has answered and before the answer is stored. Looping the kill
here reproduced that in 3 runs of 40, and in each, the repeated call had no stored answer at the
kill. No platform can avoid that window, and M1 exit 2 allows it: it promises that no *completed*
step runs again and no model call of a completed step is repeated. `research#1` and `research#2`
were still running. The test asserted something stronger, that no call at all is repeated, so it
failed whenever the kill landed in that window, more often on GitHub's runner than here.

The test now checks what the criterion and ADR-005 promise:

- no completed step runs again;
- no call of a task that finished before the crash is made again;
- no call whose answer was stored before the crash is made again;
- any other call is made at most once more.

It passed 50 runs in a row here. With reuse of stored calls turned off, 7 of 10 runs failed, so it
still guards reuse; other tests guard reuse deterministically. CI stays red until this is pushed.

### 2. A task that waited for a person left a hole in its trace (fixed)

When a tool call needs an approval, the task ends with `task.suspended`, and a new task carries
on after the answer (ADR-016). The tracer ended an agent's span only on `task.completed` or
`task.failed`, so the first task's `invoke_agent` span was never exported. Its model calls pointed
at a parent that Jaeger never received. Its waiting tool call also stayed open in the tracer's
memory for the life of the process, one entry per approval.

Now `task.suspended` ends the span, marked `aios.suspended`, and closes the tool calls still open
under it (`aios.observability.tracing`). This affected every agent that waits for a person, not
only LangGraph ones.

### 3. The threat model wasn't updated in Phase 3 (addressed)

[threat-model.md](../threat-model.md) was last reviewed on 1 November (WP 2.3). Its own rule is
that a change adding a way in changes the file, and Phase 3 added several:

- signed webhook entry points, and App code pulled from registries (WP 3.1);
- graders that ask a model to judge untrusted output (WP 3.4);
- signed action links in Slack, Teams and email, approving with changes, and people as
  capability providers (WP 3.5);
- framework adapters, which run another framework's code in the agent's process (WP 3.6);
- the OpenAI-compatible endpoint and the MCP server, through which an AI assistant drives an App
  for its user (WP 3.7).

[security-review.md](../security-review.md) kept pace, with an enforcement point for each.
But the threat model names none of the AI-specific threats these bring, or the cases for them. One
example is an assistant fooled by a web page into asking an App for a refund. R11's mitigation,
"threat model updated every phase", wasn't followed.

**Addressed on 7 November.** The threat model now has a row for each of these ways in, naming the
tests or corpus cases that prove its control, or a gap where nothing proves or stops it yet. Its
OWASP section names them too. Six corpus cases were added, and each fails with its control taken
out:

- II-10: a signed webhook asks for a refund, and a person is asked, because a webhook's run is
  tainted.
- DI-07 and DI-08: an assistant, fooled by a page, asks through `/mcp` or `/openai/v1` for a $5000
  refund with its person's key. The App's rule still asks a person. The corpus can now send a
  request through either door.
- II-11: an App that sets `untrustedInput: true` makes even a $20 refund from an assistant a
  person's decision.
- PL-05 and PL-06: an agent can't forge a person's answer to a task or an evaluation's grade.

PL-01 to PL-03 were found to pass with the reserved-domain rule taken out: their forged events
were malformed, so schema validation refused them first. They now carry well-formed payloads, and
fail without the rule.

What no corpus case covers is recorded as a gap: a fooled judge gives a wrong score; an adapted
agent's own Python tools run outside Tool OS; whoever holds a forwarded approval link answers as
its person; an assistant gets whatever is within its person's limits; and bundles aren't signed.

### 4. ADR-013 and ADR-014 weren't drafted

Both were due during Phase 3 (plan §11). ADR-014 (agent isolation and sandboxes) is what four
open items wait on:

- agent code pinned per version (FR-APP-04);
- forking with an older agent version (WP 3.3);
- a hostile handler (threat model);
- untrusted code execution (threat model).

ADR-013 (the NATS topology) opens Phase 4.

**Addressed on 7 November.** Both are drafted as *Proposed*, each with options and a
recommendation, for the owner to decide:

- [ADR-013](../adr/ADR-013-nats-topology.md): PostgreSQL stays the record, and a relay copies it to
  NATS JetStream for delivery.
- [ADR-014](../adr/ADR-014-agent-isolation.md): agent code runs in runners outside the platform's
  process, and generated code in gVisor through Kubernetes Agent Sandbox.

### 5. Thirteen ADRs await the owner's review

ADR-011 and ADR-015 to ADR-025 are each implemented and marked *Proposed*. Phase 3 work rests on
ADR-019 to ADR-025. ADR-013 and ADR-014 are drafts, not built.

### 6. The Trace Explorer shows no prompts

WP 3.2's description promised per-node prompts. FR-OBS-03 asks for inputs and outputs, which the
Trace Explorer shows, with each model call's model, tokens, cost and time. A model call's record
keeps its completion (ADR-010) and a hash of the request, not the prompt. Showing prompts would need
a decision to store them, which costs space and puts `restricted` data in one more place. M3
doesn't need it: the seeded bug was found from a step's input and output.

### 7. Scope left open within Phase 3

Of the 37 requirements Phase 3 touches, 28 are done. Nine are started, and six of those have their
Phase 3 part done, with the rest assigned to Phase 4 or 5. Three have Phase 3 parts still open:

| Requirement | What is open | Waits on |
|---|---|---|
| FR-APP-04 | Agent code pinned per App version; an upgrade runs in-flight runs' remaining tasks on the new code | ADR-014 |
| FR-CP-01 | `Tool`, `ToolProvider`, `ModelProvider`, `MemoryStore` and `Tenant` as declarative resources | Nothing; not started |
| FR-INT-01 | Agents exposed on their own through MCP; MCP prompts and resources as a client | Nothing; not started |

And from the work packages' own notes:

- WP 3.4: the console has no experiments page, and groundedness is lexical.
- WP 3.5: the real Slack step (Y8) hasn't been run, the console has no page to manage groups, and
  groups that the identity provider asserts can't be listed.
- WP 3.6 and 3.7: streaming model output and token streaming; MCP prompts.
- WP 3.7: the 2026 MCP handshake is unproven over real HTTP on this machine, because Norton
  rewrites its replies. It works in process.

### 8. No real model has been measured

Every acceptance test runs on recordings or stand-in models, so they say how the platform behaves,
not how well an App answers. The AI Researcher's recording was made through the real runtime with a
stand-in provider. The nightly contract tests against live providers (plan §8) aren't set up.

## Risk register, re-estimated

Likelihood / impact, as in plan §10.

| # | Risk | At the start | Now | Why |
|---|---|---|---|---|
| R1 | Scope creep | High / High | Medium / High | Phases landed in order, each with its tests. Phase 3 still left the scope in finding 7, and the threat model fell behind until this review |
| R2 | Workflow engine harder than expected | Medium / High | Low / High | Replay, fork, retry, loops, people and evaluations are all built on it, and the kill test still passes |
| R3 | Hyperscaler and open-source runtimes make AIOS look redundant | High / High | High / High | Unchanged: no design partner has seen it. M3 exit 1 exists to test this |
| R4 | The PostgreSQL fabric hits limits early | Medium / Medium | Medium / Medium | No benchmark since M1 (999 events/s): CI's benchmark step never ran, because the test step before it failed |
| R5 | Prompt injection can't be fully solved | High / High | High / High | The controls hold against a fooled model, including one driving an App through MCP or the OpenAI endpoint. Finding 3 records what isn't covered |
| R6 | Provider API churn | High / Medium | High / Medium | Seen: the MCP SDK 2.x and OpenAI SDK 3.x changed their shapes during Phase 3 |
| R7 | Standards churn | Medium / Medium | High / Medium | MCP's 2026 handshake arrived during WP 3.7. Both handshakes are served |
| R8 | Flaky tests from model nondeterminism | High / Medium | Medium / Medium | Recordings and stand-ins keep the suite deterministic. The flaky test found was about timing, not models: the kill test (finding 1). One socket timeout (WinError 121) was seen in WP 3.7 |
| R9 | LLM spend in development and CI | Medium / Low | Low / Low | Nothing in CI calls a model |
| R10 | Python performance on the hot path | Low / Medium | Low / Medium | Not measured since M1 |
| R11 | Security added too late | Medium / High | Medium / High | The security review kept pace. The threat model fell behind and was brought up to date at this review. The independent review (M2 exit 7) isn't arranged |
| R12 | Name or trademark conflict | Medium / Medium | Medium / Medium | The search (Sprint 1 backlog) is still open |
| R13 | **New.** How well real models do is unmeasured | — | High / Medium | Finding 8. Record the reference apps with a real model before design partners try them, and set up the nightly live tests |
| R14 | **New.** Work exists only in one working tree | — | Medium / High | Nothing after `e743f97` is committed. Commit and push |

## Roadmap, re-estimated

The plan, written for a team of four, has Phase 3 ending on 2 April 2027. Its code is built on
7 November 2026, but the milestone is open. What is left is mostly not code, so its pace is the
owner's:

| Item | Who | Size |
|---|---|---|
| Commit and push the working tree, and see CI's PostgreSQL job pass | Owner | Minutes |
| Decide ADR-011 and ADR-013 to ADR-025 | Owner | A day or two |
| Record the reference apps with a real model, then set `onDrift: fail` | Team, with a model key | A day |
| Run Y8 with a real Slack workspace | Owner or team | An hour |
| Design-partner script, target list and three sessions (exit 1) | Owner or product | Weeks |
| The independent security review (M2 exit 7) | Owner arranges | Weeks |

Done since the review was written: the cause of the red CI job, found and fixed in the tree; the
threat model, brought up to date; ADR-013 and ADR-014, drafted.

Recommendation: start no Phase 4 code until CI is green on GitHub and ADR-013 and ADR-014 are
decided. Phase 4 builds directly on both.

## Decisions for the owner

1. Accept, change or reject ADR-011 and ADR-013 to ADR-025.
2. Arrange the design partners for exit 1, and the independent security review for M2 exit 7.
3. Choose what comes next: the items above before Phase 4, or something else.

## Sign-off

| Exit | Owner's decision | Date |
|---|---|---|
| 1 | | |
| 2 | | |
| 3 | | |
| 4 | | |
| 5 | | |
| 6 | | |
| M3 | | |

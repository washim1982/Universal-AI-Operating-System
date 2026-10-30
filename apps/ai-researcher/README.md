# Reference app: AI Researcher

**Proves (M1 "First Light"):** planning, parallel fan-out, tools, model routing and durability.
Read-only, so low risk. Built in WP 1.8; specified here in Phase 0 (WP 0.5).

Manifests: [manifests/](manifests/) (validated in CI by `tests/unit/test_docs.py`).
Acceptance tests: [tests/e2e/test_ai_researcher.py](../../tests/e2e/test_ai_researcher.py),
marked expected-to-fail until their work packages land.

## Shape

`research-report` workflow, reproducing the original event chain (requirements §4.1):

```
plan (planning) → research ×N (research.web, planner-driven fan-out, ≤ 5 in parallel)
  → analyze (analysis) → draft (writing) → review (review; "reject" loops back to draft, ≤ 2×)
```

Agents: `planner-agent`, `research-agent`, `analyst-agent`, `writer-agent`, `reviewer-agent`.
Tools: `search.web.query`, `http.page.fetch` (domain allowlist).

## User stories

| # | Story | Acceptance criteria | Test | Lands in |
|---|---|---|---|---|
| R1 | As an analyst, I ask a question and get a report with cited sources. | The run completes; the report has a summary and ≥ 1 source URL per claim section; `run.completed` is the last run event. | `test_answers_a_question_with_cited_sources` | WP 1.2, 1.3, 1.4, 1.5, 1.8 |
| R2 | As an analyst, I can watch the research happen. | The run's events form one causal DAG (every event except `app.request.received` has a `causationid` in the run) in the order of requirements §4.1, with ≥ 2 parallel `task.requested` for `research.web`. | `test_event_chain_matches_the_reference_flow` | WP 1.3, 1.6 |
| R3 | As an operator, I can kill the worker mid-run and lose nothing. | After `kill -9` during the fan-out and a restart, the run completes; no completed step re-executes; no model call for a completed step repeats (M1 exit 2). | `test_survives_a_worker_crash_without_repeating_work` | WP 1.1, 1.3 |
| R4 | As a platform owner, I can move the planner to another provider by editing YAML. | Changing the `planning` rule in the model policy changes `model.call.completed.provider` for the plan step; no code changes (M1 exit 3). | `test_model_policy_change_reroutes_the_planner` | WP 1.4 |
| R5 | As a developer, I can add a third-party MCP server's tools with one config entry. | After registering an MCP server, the research agent's tool call is served by it (M1 exit 4). | `test_mcp_tools_are_usable_after_one_config_entry` | WP 1.5 |
| R6 | As a platform owner, runaway agents can't loop forever. | Two agents that trigger each other stop at the hop limit with `budget.exceeded` (M1 exit 6). | `test_mutually_triggering_agents_stop_at_the_hop_limit` | WP 1.1, 1.2 |
| R7 | As a finance owner, I can see what each run cost. | Every `model.call.completed` carries tokens and cost; the run total equals their sum (NFR-14). | `test_run_cost_is_the_sum_of_model_calls` | WP 1.4, 1.6 |

Model calls in these tests use recorded cassettes (implementation plan §8); live-provider runs
happen nightly.

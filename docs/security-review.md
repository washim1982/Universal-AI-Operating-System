# Security review: enforcement points

M2 exit criterion 7 asks for a review of every enforcement point. This is the record, kept current
as work packages land. It is a self-review by the team that built the code, so **an independent
review is still required before M2 exits**.

Last reviewed: 2 Nov 2026, WP 2.1 to WP 2.4, and WP 3.1 to WP 3.3.

AI-specific threats, and what stops each one, are in [threat-model.md](threat-model.md).

## Enforcement points (FR-SEC-04)

| Point | Where | What decides | Status | Proof |
|---|---|---|---|---|
| Control-plane API | `aios.api.app.requires` | Built-in roles, through Cedar | Enforced on every route | `test_no_route_answers_without_a_credential` fails if a route has no gate |
| Tool invocation | `aios.tools.service._authorize` | The App's policy set, for the agent and its delegate | Enforced | `tests/unit/security/test_enforcement.py` |
| Task dispatch | `aios.kernel.worker._authorize` | Kill switch, then the App's policy set | Enforced | `test_a_suspended_agent_gets_no_work_and_the_run_says_why` |
| Model routing | `aios.models.kernel._authorize`, `_standing` | Kill switch, then the App's policy set, then budgets; the data-class guard in the router, which a downgrade also passes through | Enforced | `test_a_suspension_stops_a_model_call_already_on_its_way` |
| Publish | `aios.kernel.context.Context.emit` | Platform domains are refused | Enforced for agent code | `test_an_agent_can_not_forge_a_platform_event` |
| Subscribe | `aios.kernel.worker` | An agent's scope is its own tenant and App | Enforced by scope, not by a policy | `tests/isolation/` |
| Memory read and write | `aios.memory.service._authorize` | The App's policy set, per scope, for the agent and its delegate | Enforced | `tests/unit/memory/test_service.py` |
| Memory administration | `aios.api.app.requires` | Built-in roles, through Cedar; global memory needs `platform-admin` | Enforced | `tests/unit/api/test_memory_api.py` |
| Approval decisions | `aios.controlplane.runtime.decide_approval`, then `aios.human.service.decide` | Built-in roles and the kill switch through Cedar, then the rule's approvers and separation of duties | Enforced. A refused attempt is on the audit record | `tests/unit/api/test_approvals_api.py`, `tests/unit/human/` |
| Egress | `aios.tools.service._refused`, then every hop in the provider | The App's and the tool's lists under the installation's ceiling. An approval can't lift it | Enforced for URLs, once an App declares a list | `tests/unit/tools/test_egress.py`, `tests/redteam/` |
| Taint | `aios.workflow.state` folds it, `aios.tools.service` and `aios.memory.service` pass it to policy | Default rule in `app-defaults`, unless the App exempts a tool | Enforced | `tests/unit/security/test_taint.py`, `tests/redteam/` |
| Consent | `aios.security.enforcement.consent_policy`, installed with the App's policies; data classes in `Runtime.chat` and the engine's `_start_from`; hosts in `_granted_egress` | What a tenant admin granted. A forbid, so the App's own policies can't undo it | Enforced for tools, memory, data classes and hosts | `test_a_tool_left_out_is_blocked_at_runtime`, `test_an_apps_own_policy_cant_grant_itself_more`, `test_an_event_of_a_class_left_out_starts_nothing` |
| Installation | `aios.api.app` (`app.install`, `app.manage`), `aios.controlplane.installer` | Only `platform-admin` offers code; `tenant-admin` agrees. A bundle must match its digest, and a version can't change | Enforced | `test_a_forged_bundle_is_refused`, `test_a_version_never_changes`, `tests/unit/api/test_authorization.py` |
| Webhook entry points | `aios.controlplane.runtime.receive_webhook` | An HMAC over time and body with the App's declared secret, within five minutes, once | Enforced. Every failure is the same 401 | `tests/unit/controlplane/test_entrypoints.py` |
| Console reads | `aios.api.app`: `/v1/costs` (`event.read`), `/v1/apps/{app}/policies` and its simulator (`audit.read`), `/v1/agents` (`app.read`), traces (`run.read`) | Built-in roles, through Cedar, as for every route | Enforced; the console has no permissions of its own | `test_roles_over_http`, `test_an_auditor_can_ask_the_simulator` |
| Memory corrections | `aios.memory.service.update_as` | `memory.manage`; a version that has moved on, a legal hold, or global memory without `platform-admin` is refused | Enforced, and audited as `memory.item.updated` | `tests/unit/api/test_memory_api.py` |
| Approved actions | `aios.tools.service._authorize` | The App's policy set again, with the approval | Enforced: an approval lifts `requireApproval` only | `test_a_rule_added_during_the_wait_still_applies`, `test_the_kill_switch_outranks_an_approval` |
| Replay, fork and retry | `aios.api.app` (`run.start` for a replay or fork, `run.control` for a retry); `aios.core.replay.holding_back` in `aios.tools.service`, `aios.memory.service` and `Context.emit` | A replay answers every call from the record. A fork holds back every tool that isn't `READ`, memory writes and erasures, and emits. The App's policy set still decides each held-back call, and nobody is asked | Enforced. A worker learns the mode from the task it is given, not from the agent | `tests/unit/controlplane/test_replay.py`, `test_a_retry_needs_someone_who_controls_runs` |

## Storage isolation (FR-SEC-06)

Reads for one tenant run as the `aios_tenant` role, with row-level security on every table that
holds tenant data. `test_every_table_is_covered` fails if a table is added without it, and
`test_a_query_with_no_filter_sees_one_tenant` shows PostgreSQL hiding other tenants' rows on a
query with no tenant filter. `test_memory_searches_with_no_filter_see_one_tenant` shows the same
for full-text and vector search. Global memory is the only thing tenants share, by design, and
only a `platform-admin` can write it.

## Known gaps

| Gap | Risk | Plan |
|---|---|---|
| Tenants share one process, and a manifest names a Python entrypoint | Whoever can install an App can run code next to every tenant, so only `platform-admin` can | Sandboxing, ADR-014 |
| `requireApproval` on anything but a tool is a denial | Safe, but FR-HUM-01 asks for any governed action. Approving a memory write would put its content where `forget` can't reach it | Decide with design partners |
| A person with two identities counts as two people | Someone with a sign-in and an API key could approve their own action, if both hold the role | A directory that links identities |
| An approval's payload stays after `forget` | Tool arguments are in `approval.requested`, `tool.call.requested` and the `approvals` table | Log retention and erasure, FR-EVT-14 |
| An agent's rationale is shown to an approver | It is written by a model that may have read injected content, so it can be written to persuade. It is labelled as a claim, and `tainted` says so | The console must render it as text (WP 2.6) |
| Only `ctx` calls replay when a task resumes | Anything else a handler does before the line that asks happens again | Documented in ADR-016; handlers already had to tolerate redelivery |
| A run's model calls keep their output in the log | `forget` erases memory, but text a model wrote from an erased item stays in `model.call.completed` | Log retention and erasure, FR-EVT-14 |
| A vector can't be proven unrelated to its source text | Embeddings are personal data if the text was. They are erased with the item, but a backup keeps both | Backup retention is an operator decision; document it in WP 4.x |
| Session ids can be reused by anyone in the tenant | A caller who knows a session id can join it and read its session memory | Per-run ownership, with the member gap below |
| Allowed reads of the API aren't in the audit log | Only denied reads are, apart from memory | Decide with design partners |
| Event positions are global across tenants | A tenant can infer how busy the installation is from gaps in the numbers | Accepted for now; per-tenant cursors in WP 4.1 |
| An operator with full database access can rebuild the whole audit chain and its head | The chain proves integrity against anything short of that | Export heads off the system (FR-SEC-08 P1) |
| Failed sign-ins aren't rate limited | API keys can't be guessed, but the attempts cost a query each | Rate limiting at the gateway, or in WP 4.x |
| A member can read every run in the tenant | No per-run ownership | ADR-011 records this; decide with design partners |
| Secrets are masked as exact copies only | A tool that encodes a secret can still leak it to a host on its list | DLP, FR-SEC-11 (P1) |
| MCP connections take no credentials | Zendesk can't connect | WP 2.7 |
| An agent's code isn't pinned per version | A task that was running during an upgrade carries on with the new code | Agents in their own processes, ADR-014 |
| A new entry point needs no consent | A version that adds a schedule or a webhook, and nothing else, runs at once. Its runs have only what was granted | Listed under `starts`; decide with design partners whether it should ask |
| `/v1/hooks` is open to anyone who can reach the API | Each request costs an HMAC and a database row; a flood can't start runs, but it costs | Rate limiting at the gateway, or in WP 4.x |
| A registry is trusted for availability, not content | A digest proves the bundle is the one agreed to, not who made it | Signing before the marketplace (Phase 5) |
| A trace leaves restricted data out, but the run's events don't | Whoever may read a run can still read `restricted` values in `GET .../events` | Decide with design partners whether reading a run's restricted data needs a role of its own |
| Run controls show to anyone who can read the run | Someone without `run.control` sees Pause and Cancel, and is refused when they press one | The API decides; the console could ask it first |
| A fork runs `READ` tools for real | A tool marked `READ` that does change something would do it again in a fork. The label is the tool author's claim | Review side-effect labels when a tool is registered; a fork option that holds back reads too |
| A replay or fork reads memory as it is now | What an agent recalls can differ from what the source saw. Policy decides each read afresh, so nothing is read that the caller couldn't read | A divergence shows it; record what was recalled, FR-EVT-14 |
| A member can replay or fork any run they can read | The new run copies outputs they could already read, and runs as them, not as the source's user | Follows the per-run ownership gap above |
| `aios dev` and `aios api --no-auth` make the caller a platform admin | Local only: both refuse a non-loopback address | By design (ADR-011) |

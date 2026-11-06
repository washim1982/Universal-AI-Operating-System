# ADR-018: Budgets are checked before a call, against a ledger paid in one step with the event

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-01
- **Deciders:** Tech lead
- **Requirements:** FR-MOD-02, 04, 08, 09; M2 exit 4

## Context

FR-MOD-08 asks for hard and soft budgets per run, App, tenant and user. A soft limit raises an alert
and downgrades the route; a hard limit denies the call.

The Workflow Engine already stopped a run between steps, using a total folded from the run's log.
That can't do the job on its own. One long task can spend any amount between two steps, and "what
has this tenant spent this month" isn't in any one run. Adding up the log before every model call
would read a month of events each time.

## Decision

**Spend is kept in a ledger, charged in one step with the event that records the call.**
`SpendLedger.charge` adds the cost to each payer and publishes `model.call.completed`, through the
fabric's outbox on PostgreSQL. So a call is never on the record without being paid for, a crash
can't count one twice, and every worker reads the same totals. A task that runs again reads its
recorded call back from the log, and pays nothing. The log stays the source of truth: the ledger
could be rebuilt from it.

**A call has up to four payers:** its run, the person it is for (`onbehalfof`), its App and its
tenant. All four are charged whether or not a limit exists, so that a limit added on the 20th counts
what the month has spent. Monthly totals are keyed by the UTC month.

**Each limit in a policy is hard; the soft limit is a fraction of it.** `perRunUSD`,
`perUserMonthlyUSD`, `perAppMonthlyUSD` and `perTenantMonthlyUSD`, with `softAt` (default 0.8). An
App's own `spec.budgets` can tighten its policy, never loosen it. The tenant limit comes from the
policy of the App making the call.

**Reaching a hard limit denies the call** before it is routed, so a run stops with
`budget.exceeded`, naming the level, and the engine doesn't retry it.

**Reaching a soft limit publishes `budget.warning` once per payer and period**, then does what
`onSoftLimit` says: `warn`, `deny`, or `downgrade`.

**A downgrade is an ordinary policy rule**, `match: { budget: soft }`. It applies only once a soft
limit is reached. Using the rule list keeps three things that a second mechanism would have lost:
first-match order, so that a rule for restricted data placed above it still wins; the data-class
guard, which every candidate passes through; and `require`.

**With no such rule, the cheapest candidate goes first.** The policy in requirements 7.10 says
`onSoftLimit: downgrade` and has no soft rule, and a downgrade shouldn't silently do nothing. The
candidates are the route's own, so nothing can be chosen that the policy didn't already allow, and
the guard has already run. A model with no price goes last: unknown isn't the same as cheap.

If no candidate of a soft rule is usable, the normal route is used. A soft limit never stops work by
itself.

**Prompt caching is asked for where it pays, and counted everywhere.** On Anthropic the system
prompt carries a marker, and a conversation of more than one message also gets automatic caching.
One question isn't marked, because it won't come back and would only pay the write premium. OpenAI
and Gemini cache by themselves; their adapters report what they found. `Usage` gains
`cache_read_tokens` and `cache_write_tokens`, parts of `input_tokens`, and the registry gains
`cacheRead` and `cacheWrite` prices, so a budget counts what a cached call really cost.

**Restricted data is never offered to a cache.** A cache holds a prompt for a while, and that is
retention, however short. The adapter gets the strictest label the kernel knows, not the one the
request gave itself. This only covers what we control: an OpenAI or Gemini deployment caches on its
own terms, so its `zero-retention` contract has to cover that.

**A deployment supplies its own registry with `AIOS_MODEL_REGISTRY`.** Its entries add to the shipped
ones or replace them by id. Without this a `<placeholder>` could never be filled in, and a private
model could never have a price.

## Alternatives considered

- **Add up the log before each call.** Always right, and needs no table, but its cost grows with
  the month.
- **A subscriber that keeps the totals.** One more moving part, and totals that lag behind the
  calls they are meant to limit.
- **A reservation before each call.** See below.
- **A `downgradeTo` list on the policy.** Simple, but a second way of routing that the guard and
  rule order would have had to learn about.
- **Only cheapest first, with no rule.** Automatic, but an author can't then say which model takes
  over, and cost isn't the only reason to pick one.
- **Refuse a policy that says `downgrade` with no rule.** Clear, but it would reject the policy in
  the requirements.

## Consequences

- **A limit can be passed.** The check comes before the call and the charge after it, so calls in
  flight at the same time can all pass the check. The overshoot is at most what those calls cost.
  A reservation would close this, but needs a cost estimate before the model is chosen, and would
  refuse calls that would have fitted. The limit is a stop, not a guarantee.
- A hard limit also denies calls to a free private model, because the rule is about the budget and
  not the bill.
- Run totals stay in the ledger after the run has ended. Nothing prunes them yet.
- A model with no price costs nothing, so budgets can't see it until the registry gives it one.
- Warnings are events and a metric (`aios_budget_warnings`). Sending them somewhere, such as email
  or Slack, is WP 3.5.
- Changing the shipped default policy's `perRunUSD: 2.00` from a document into something enforced
  can stop a run that used to finish.

## Implementation

`src/aios/models/ledger.py`, `postgres.py`, `kernel.py` (`_standing`, `_charge`), `router.py`,
`gemini.py`; `tests/unit/models/test_budgets.py`, `test_caching.py`, `test_gemini.py`,
`test_providers.py`; `tests/conformance/test_spend_ledger.py`.

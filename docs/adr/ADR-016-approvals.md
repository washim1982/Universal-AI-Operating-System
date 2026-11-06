# ADR-016: A task waits for a person by stopping, and runs again once they answer

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-01
- **Deciders:** Tech lead
- **Requirements:** FR-HUM-01 to 05, 07, 10; FR-TOOL-05, 07; NFR-06; M2 exit 2 and 3

## Context

A refund above a threshold has to wait for a person, and that person may take a day. The decision
to refund is made in the middle of an agent's handler, which is a Python coroutine: it can't be
saved, and keeping it in memory for a day would hold a worker slot and die with the process.
FR-HUM-10 asks for the opposite: a paused run uses no compute and survives restarts and deployments.

The approval also has to be for the thing that was shown. If a person approves $120, the worker must
not be able to spend that yes on $5,000, and a refund must happen once however many times the
machinery around it restarts.

## Decision

**The task stops, and runs again.** A tool call that needs an approval raises `AwaitingApproval`.
The worker publishes `task.suspended`, acknowledges the task and forgets it; the engine marks the
step as waiting. Once someone answers, the engine requests the task again, whatever the answer was,
naming the earlier executions in `resumes`. The handler runs from the top, and what it already did
replays from the log, which is how a task already recovers from a crash (ADR-005). It reaches the
same line, and there it gets the answer: the call goes ahead, or `ApprovalDeniedError` is raised for
the agent to catch. ADR-006 rules out replaying *workflow* code; this replays only a step, as M1
already did, so it stands.

**`AwaitingApproval` is not an `Exception`**, for the reason `asyncio.CancelledError` isn't. A handler
with `except Exception` around a tool call must not carry on as if the call had failed. The context
also keeps the fact, so a task whose handler catches it anyway (`gather(return_exceptions=True)`
does) still can't report that it finished.

**An approval is for one call.** Its id is derived from the task, the place in the task and a hash
of the action and arguments. A resumed task that asks for anything else gets a different id, and so
a new question for a person. A retried step is a new call too, and is asked again.

**An approval lifts `requireApproval` and nothing else.** Policy is asked again with it, at the time
of the call, so a `deny` added during the wait still applies, and so does the kill switch.

**Decisions happen in one place.** `approvals` is a table, and closing a row publishes the event in
the same transaction, through the fabric's outbox. Answers, timeouts and cancellations all go through
that one step, so of two answers one wins, and only one reaches the run. Requests still start as
events, because the engine writes workflow-step approvals into an atomic batch and can't reach
Human OS; a consumer copies them into the table.

**Silence means no.** Every approval has a deadline, 24 hours unless a rule or step says otherwise.
`onTimeout: approve` needs a rule or step that says so, and where several rules apply, every one of
them must, and the shortest time applies. An answer after the deadline is refused even before the
sweep closes the row, so the deadline doesn't depend on when the sweep runs.

**A denial is final.** A failure caused by an approval isn't retried, although steps retry by default.
Retrying a no would mean asking until someone says yes.

**Whoever an action is for can't approve it,** whatever roles they hold, and nobody can unless a rule
names them. Being a tenant admin isn't enough on its own. An approver's rule is the only source of
authority: `approval.decide` only lets a caller reach the check.

**A webhook says where to look, not what is there.** It carries the id, the tool and the risk, signed
with HMAC-SHA256 over a timestamp and the body. The receiver fetches the rest with its own
credential, so `restricted` content doesn't leave in a notification.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep the handler waiting in memory | Fails FR-HUM-10: it holds a slot, and a restart loses it |
| Approvals as workflow nodes only | An agent decides on a refund at run time, so the graph can't know. FR-HUM-01 asks for a policy outcome, and the injection test (M2 exit 3) depends on it |
| Fail the task and let the retry find the approval | A retry is a new attempt: its model calls run again and can reach different arguments, so the approval has nothing to bind to. It would also spend a retry on every approval |
| Keep approvals only in the log | The log filters by type after reading rows, so an inbox would read every event of the tenant. It has no conditional publish either, so nothing could stop two answers |
| Keep approvals only in a table | Then the run hears about a decision by polling or by a second write that a crash can lose. The outbox avoids that |
| Let the engine run the expiry clock | The engine publishes from its own process and can't take part in the closing transaction, so an expiry could race a person's answer |

## Consequences

- **Only `ctx` calls replay.** Anything else a handler does before the line that asks happens again:
  ADR-005 already asks handlers to tolerate this, but it used to take a crash, and now it takes an
  approval. `ctx.emit` and memory writes became idempotent for this reason. Memory reads are live on
  the second run, and embeddings are made and billed again.
- **A step's `timeout` and the run's `maxDuration` count the wait.** The Support Desk sets 25 hours for
  this. A run that hits either fails with its approval still open, which then closes as cancelled.
- **`onTimeout: escalate` is gone** until escalation exists (WP 3.5). Accepting it and denying would
  have been a quiet lie.
- **An approver needs a built-in role too,** usually `member`, because custom roles grant no
  control-plane action (ADR-011).
- **A step that fails after its tool succeeded asks again** on the retry. `ctx.step_key` no longer
  changes between attempts, so a provider that honours idempotency keys refunds once.
- **Two identities defeat separation of duties.** A person with an API key and a sign-in looks like
  two people. Fixing it needs a directory that links them.
- **`requireApproval` on anything but a tool is a denial,** and on a workflow step it is the step
  type instead. Approving a memory write would have to show its content, which would then sit in the
  log where `forget` can't reach it. FR-HUM-01 stays open for this reason.

## Implementation

`aios.core.human`, `aios.core.ports` (`CallSite.executions`, `ApprovalGateway`) and `aios.human`
(`service`, `store`, `postgres`, `webhook`). Tool OS asks through the port; the engine's part is
`deliver_decisions`, which scans rather than reacts to one event, because an answer can overtake
`task.suspended`: they are separate consumer groups.

Tests: `tests/unit/human/test_pausing.py` runs the real engine, kernel, Tool OS, Security Kernel and
Human OS. `tests/conformance/test_approval_store.py` runs on memory and PostgreSQL, and
`tests/isolation/` covers both tenants. Part 12 of the testing guide walks through it by hand.

Not built: approve with edits (FR-HUM-06), reminders, escalation, groups and quorum, Slack, Teams and
email (all WP 3.5), and the console inbox (WP 2.6). `approval.modified` and `approval.escalated` stay
in the catalog, unpublished.

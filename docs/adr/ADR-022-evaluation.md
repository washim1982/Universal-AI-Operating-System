# ADR-022: Evaluations are runs of the App, graded and gated, recorded as events

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-05
- **Deciders:** Tech lead, AI engineering
- **Requirements:** FR-OBS-07 (evaluation framework), FR-MEM-12 (retrieval quality), FR-CP-03
  (`aios eval`); M3 exit 3; ADR-008, ADR-017, ADR-021

## Context

FR-OBS-07 asks for datasets, rule, schema, model and human graders, experiments compared across
versions, a CI gate, and a sample of production runs graded online. FR-MEM-12 asks for recall@k,
MRR and groundedness on golden datasets. M3 exit 3: the CI gate blocks a prompt change that lowers
the AI Researcher's score.

CI has no model keys. The AI Researcher's tests answer model calls from a recording (a cassette),
and a cassette answers by call site whatever was asked. So a prompt change, graded in CI, would be
graded on the old prompt's answers. The recording was also found to predate the current prompts:
five of its ten calls were made for requests that have since changed.

## Decision

**A suite is an `Evaluation` manifest, run rather than installed.** It names its cases (an input,
and optionally an expected answer), its graders, and its gate: a minimum score, how far below a
baseline it may fall, and whether a stale recording fails it. It lives beside the App, such as
`apps/ai-researcher/evals/quality.yaml`, outside the bundle.

**Graders:** `exact`, `contains`, `regex`, `schema`, `judge` (a model scores against a rubric from 0
to 10), `grounded` (the share of the answer's sentences whose words appear in what the run read:
its tool outputs and the memory it recalled), and `human`. Each looks at a dotted `path` in the
output and has a weight. A case scores the weighted mean of its grades, and an experiment the mean
of its cases. A suite with `retrieval` instead searches the App's memory for each case's query and
scores recall@k, reciprocal rank and, given an answer, its groundedness in what was found. Results
are counted by document, not by chunk.

**An experiment runs each case as an ordinary run of the App.** It runs on behalf of whoever asked,
so their permissions apply. It is a sequence of events in the App's scope under the experiment's
id: `eval.experiment.requested` (a command, which one worker takes), then `started`,
`case.started`, `case.completed`, `case.graded` and `completed`. A worker that takes an experiment
over after a crash reads those events and carries on: it starts no case twice, and waits for a
run it had started. While an experiment runs, its worker keeps the lease on the request.

**A judge is the App's `aios-evaluator` agent.** Its calls go through the Model Kernel. Cedar
places any agent principal inside the App it acts for, so the App's model policy, budgets, consent
and data class apply to a judge as to the App's own agents, and the judge's cost is the App's. What
it grades is quoted as untrusted data (ADR-017), and the system prompt tells it to ignore
instructions inside. A retrieval search runs as the same agent, so policy decides what it may
read.

**A person grades later.** A `human` grade is pending until a reviewer the grader names (`role:` or
`user:`) gives it, through the API or `aios eval grade`. An experiment with a pending grade can't
pass its gate.

**Drift is reported, and can fail the gate.** The cassette now says when a recorded answer was
made for another request. The model call records it as `drifted`, a case counts its drifted calls,
and `onDrift: fail` (the default) fails the gate on any. So in CI, a prompt change can't pass on the
old answers: the developer records new ones with a real model (`--cassette-mode record`), and the
gate grades those against the committed baseline. The AI Researcher's suite says `onDrift: warn`
until its recording is renewed, as its comment explains.

**The gate is the exit code.** `aios eval run` exits 0 when the experiment may ship and 1 when it
may not, giving every reason: below the minimum, below the baseline by more than allowed (overall
or for any grader or metric), drift, a pending person, or not completed. With `--app-dir` it runs in
the same process with the App's folder, which is what CI does; without, the server runs it on the
installed App. `--out` writes the summary that becomes the next baseline.

**Online evaluation samples finished runs by their id.** An App's `spec.evaluation.online` gives a
share and graders that need no expected answer: no `human`, and `exact` only with a value. Each
worker hears `run.completed`, and one grades a run in the sample. It records `eval.run.graded` on
the run itself, so the run's page shows its grade, and a redelivery grades nothing twice.

**Permissions.** `eval.run` (operator and both admins) runs suites, because they spend money.
`eval.read` is given to members, auditors and up. `eval.grade` (members and up) is further limited
to the reviewers a grader names. Experiment requests and people's grades are on the audit log, and
agents can't publish in the `eval` domain.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Grade in the CLI only | No online evaluation, no experiments kept for comparison, and a judge would need model keys wherever the CLI runs |
| A separate evaluation service | One more thing to run. Workers already have the models, the Apps and a lease |
| Judges as an outside principal | They would need their own model policy and budget. Cedar already treats an agent as the App's |
| Semantic groundedness only, by a judge | Needs a model in CI, and gives different answers on the same input. The lexical check is cheap and repeatable, and a judge rubric can still check meaning |
| Ignore drift, as replays of tests do | Then CI would pass a prompt change on the old prompt's answers, which is exactly what M3 exit 3 forbids |

## Consequences

- M3 exit 3 holds on recordings: a changed prompt must be recorded again to pass, and the new
  answers are graded against the baseline. `tests/unit/controlplane/test_evaluator.py` blocks a
  recording that cites a blog instead of the spec.
- The AI Researcher's recording is stale at five calls. Until it is recorded again with a model,
  its suite warns about drift instead of failing on it.
- A judge in CI needs its answers recorded too, so the Researcher's suite uses rule, schema and
  grounding graders only.
- Groundedness is lexical: it finds sentences with nothing behind them, not wrong ones.
- The console doesn't show experiments yet. No FR-UI requirement asks for that page; the API and
  `aios eval` cover it.

# ADR-023: People in the loop: quorums, edits, signed links, work and feedback

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-06
- **Deciders:** Tech lead, security
- **Requirements:** FR-HUM-02, 04 to 09, FR-TOOL-07, FR-UI-04; M3 exit 5; ADR-011, ADR-016

## Context

ADR-016 made an approval one person's answer about one call, closed atomically with its event.
WP 3.5 adds what Phase 3 asks of people in the loop: several approvers (n of m) drawn from
groups, reminders and escalation, approving a changed action, Slack, Teams and email with signed
action links, people who provide a capability as an agent would, and feedback that feeds
evaluations and memory. M3 exit 5: an approval is completed from Slack with a signed link.

Each of these is a way round a check, if it is done carelessly. A quorum that counts one person
twice is a quorum of one. An edit can turn an approved $120 refund into $5,000. A link in a chat
channel can be followed by anyone in it, or by a link preview. Feedback written into memory is
read by agents later.

## Decision

**Quorum.** A rule or approval step says `quorum: n`. Each yes is an `approval.voted` event,
appended to the approval's record in one step with its event, and refused if that person has
voted already. The yes that reaches n also grants it: `approval.granted` names every voter. One no
denies it. Whoever an action is for still can't vote.

**Groups.** An approver selector may be `group:<name>`. Members come from the identity provider's
groups claim (`AIOS_OIDC_GROUPS_CLAIM`), or from the tenant's directory, which a tenant admin sets
(`PUT /v1/groups/{name}`, recorded as `security.group.changed`). An empty group is no group.

**Reminders and escalation.** `remindEvery: 30m` and `escalate: [{after: 2h, approvers: [...]}]`.
The Human OS sweep publishes `approval.reminded` and `approval.escalated`; an escalation adds its
approvers, and keeps those first asked. Each is recorded with its event only if the approval is
still as the sweep read it, so two workers sweeping at once act once.

**Approve with edits.** Only a yes can change an action, only the parameters it already has, and
not with a quorum (everyone must agree to the same thing). The approval records the edits and
publishes `approval.modified`. The tool call then checks the changed arguments against the tool's
schema and against policy again, without the approval: if that denies it, or needs an approval
rule beyond the ones this approval answered, the call is refused ("as changed, it needs another
approval"). An answer is final, so a refused edit fails the step rather than asking again. The
completed call records the arguments it was really called with.

**Previews.** A native tool may have a `previewer`: a dry run, whose result goes in the approval
request (`preview`). A preview that fails or times out is left out: it never stops the request.

**Signed action links.** Slack (a bot's direct message), Teams (an Adaptive Card through a Teams
workflow, to the person's address) and email. Each approver gets their own links, one to approve
and one to deny. A link is `v1.<claims>.<HMAC-SHA256>` under a key derived from
`AIOS_ACTION_LINK_SECRET`. Its claims are the tenant, the approval, the verdict, the person, the
channel and an expiry: the approval's, and seven days at most. Opening a link shows the action and
a button. Only pressing the button (a POST) answers, so link previews and mail scanners decide
nothing. The answer goes through the same check as any other: the person must still be an
approver, with the roles they have now, in a tenant and App that aren't suspended. It is recorded
`via` its channel. A link's page loads nothing from elsewhere, runs no script, can't be framed,
and sends no referrer. Messages carry the App, the action, the risk and the expiry. They carry the
summary only for public or internal data.

People set their own addresses (`PUT /v1/me/contact`, recorded as `security.contact.changed`).
A contact event that names anyone but its author is ignored. Otherwise an administrator could
point someone's links at themselves.

**People as capability providers.** An App's `spec.humans` lists a capability, its assignees, a
description and an output schema. A step that asks for the capability publishes `task.requested`
as usual. The Human OS consumes those for people's capabilities: it records the task and
publishes `task.claimed` and `human.task.assigned` in one step. An assignee completes it
(`POST /v1/tasks/{id}/complete`, `aios tasks`, or the console). The answer must fit the schema. The
first answer wins, closed with its `human.task.completed` and `task.completed` in one step, so the
run gets it as it would an agent's. Ending the run cancels the task. A capability is people's or
an agent's, never both: both would get every task. Seeing and doing tasks is `task.work`.

**Feedback.** `POST /v1/apps/{app}/runs/{run}/feedback` gives a rating (`good` or `bad`), a
correction and a comment, about the run or a step. It is a `human.feedback.given` event on the
run, so it shows in the trace. With `remember`, it is also written to the App's memory as the
person (`memory.write` on app memory is checked with their roles). Corrections of whole runs
become evaluation cases (`GET .../feedback/cases`, `aios feedback cases`): the run's input, with
the correction as the expected answer. Giving feedback is `run.feedback`.

## Consequences

- An approval can be answered where people already are, without a credential for AIOS. Holding
  a link is the credential for one answer, so links go to one person each, never to a channel,
  and a reverse proxy's access log is as sensitive as the links in it (AIOS itself logs no paths).
- Changing `AIOS_ACTION_LINK_SECRET` ends every link sent so far.
- A message that can't be sent is tried again with the rest. Someone may be told twice; nobody
  should be told never.
- Teams needs a workflow on the tenant's side to deliver a card to a person. Slack needs a bot
  token with `chat:write`, and no inbound access to AIOS, because the buttons are links.
- Feedback written to memory has no embedding until something writes it again, so it is found by
  keyword.
- `human.task.*` and `human.feedback.given` sit in the reserved `human` domain, so agents can't
  forge them.

## Alternatives considered

- **Slack interactive buttons** (Slack posts the click to AIOS, signed by Slack). They need
  inbound access from Slack, a mapping from Slack users to principals, and work only for Slack.
  Links work the same way in Slack, Teams and email.
- **One link per approval, in a shared channel.** Anyone in the channel could answer as anyone.
- **Letting an approver change anything.** A change could then grow past what the approver's own
  rule covers. Re-checking against policy, and refusing anything that needs more approval, keeps
  an edit within what was approved.
- **Keeping tasks only as events.** There is no first-wins guarantee in the log, and an inbox
  would replay every run. Tasks get a small store, as approvals have.

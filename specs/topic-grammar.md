# Topic grammar v0.1

ADR-003. Implementation: `aios.core.topics` (grammar and matcher) and `aios.fabric.subjects`
(physical subjects).

## Logical topics

What developers see, and what the CloudEvents `type` holds.

```
topic   = token "." token *( "." token )        ; 2–16 tokens, at most 255 characters
token   = [a-z0-9] [ *[a-z0-9_-] [a-z0-9] ]
```

- Shape: `<domain>.<entity…>.<verb>`, e.g. `research.plan.created`, `agent.lifecycle.suspended`.
- **Commands** end in `requested` (`task.requested`, `tool.call.requested`,
  `approval.requested`) and are consumed by exactly one handler — subscribe to them with a
  consumer group. **Facts** are everything else, use a past-tense verb where possible, and are
  broadcast (FR-EVT-02).
- **Reserved domains:** `app`, `run`, `step`, `task`, `agent`, `model`, `tool`, `memory`,
  `approval`, `human`, `policy`, `budget`, `system`. Apps publish under their own domains.

Capability names (`research.web`) and tool names (`github.pr.create`) use the same tokens.
Tool names always have exactly three: `<provider>.<resource>.<action>`.

## Patterns

NATS-compatible wildcards (FR-EVT-03):

| Token | Matches |
|---|---|
| `*` | exactly one token |
| `>` | one or more remaining tokens; only as the last token |

| Pattern | Matches | Does not match |
|---|---|---|
| `task.*` | `task.completed` | `task.requested.research.web` |
| `task.requested.research.>` | `task.requested.research.web` | `task.requested.writing` |
| `research.*.created` | `research.plan.created` | `research.plan.draft.created` |
| `>` | everything in scope | — |

**One grammar everywhere.** Subscription patterns, capability patterns in workflows
(`research.*`) and tool permissions in manifests and policies use these semantics. Because tool
names have three tokens, "every Zendesk tool" is `zendesk.>`; `zendesk.*` can never match a tool,
and manifest validation rejects it with a hint.

## Physical subjects

Built by the fabric; agent code never sees them (FR-EVT-04):

```
<tenantid>.<appid>.<logical topic>[.<routing suffix>]
```

- The catalog defines routing suffixes. Today only `task.requested` has one: the payload's
  `capability`, so `acme.ai-researcher.task.requested.research.web`. Subscribe with
  `task.requested.>` to see every task request; `task.requested` alone matches none.
- Subscribers pass a scope (`Scope(tenant, app)`) plus a logical pattern; the fabric prefixes
  the pattern with the scope. Tenant and app ids are single DNS-label tokens, so a scoped
  pattern can never match another tenant's subject. `Scope.platform()` (`*.*.<pattern>`) is
  reserved for platform components such as the dispatcher and the audit writer.

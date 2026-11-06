# Reference app: AI Support Desk

**Proves (M2 "Safe by Default", the MVP):** memory, write tools, approvals, injection defenses
and multi-tenancy. Built in WP 2.7.

Manifests: [manifests/](manifests/). Config schema: [config.schema.json](config.schema.json).
Policy: [policies/support-desk-policy.yaml](policies/support-desk-policy.yaml). Agents:
[src/ai_support_desk/agents.py](src/ai_support_desk/agents.py). Acceptance tests:
[tests/e2e/test_ai_support_desk.py](../../tests/e2e/test_ai_support_desk.py). To try it, see
[Part 5 of the testing guide](../../docs/testing-guide.md#part-5-ai-support-desk).

## Shape

`handle-ticket` workflow, triggered by `zendesk.ticket.created` or by chat:

```
triage (support.triage) → research (support.kb.search) → resolve (support.resolve) → reply (support.reply)
```

`refund-agent` provides `support.resolve` and may call `stripe.refund.create`. The approval gate
is not a workflow step: the policy requires approval for refunds above the App's configured
threshold, and AIOS requires it for any `financial` tool once a run is tainted by untrusted content
(ADR-017).

## Three decisions worth knowing

**A refund request doesn't read the knowledge base.** Reading taints the run, and a tainted run needs
a person for a refund, so if every ticket read the knowledge base first, a $20 refund would need a
lead. So `kb-agent` reads only for questions. A poisoned article can reach only a run that read it,
and that run asks a person, whatever the amount.

**A reply is exempt from taint** (`spec.taint.exempt`). Otherwise every answer drawn from the
knowledge base would wait for a person. That accepts a risk: a poisoned article can shape what a
reply says. It can't move money, and `reply-agent` has no other tool. A deployment that wants a
person on replies removes the exemption.

**A ticket from Zendesk stays tainted.** Nobody signed in to send it, so any refund it asks for goes
to a person, however small. A signed-in customer's own chat doesn't. The threshold in S3 applies
to them.

## Stripe and Zendesk

[src/ai_support_desk/tools.py](src/ai_support_desk/tools.py) has stand-ins. No money moves: a refund
is a line in `.aios/support-desk/refunds.jsonl`. Set
`AIOS_TOOL_PROVIDERS=ai_support_desk.tools:providers` on the worker to use them, or point it at your
own providers.

## User stories

| # | Story | Acceptance criteria | Test | Lands in |
|---|---|---|---|---|
| S1 | As a customer, I get a correct answer from the knowledge base. | The reply cites the KB article it used; the KB was ingested into App memory. | `test_answers_from_the_knowledge_base_with_citations` | WP 2.1, 2.7 |
| S2 | As a support lead, refunds above the threshold wait for my approval. | A refund above `refundApprovalThresholdUSD` pauses the run with `approval.requested` showing payload and rationale; approving executes the refund exactly once; denying produces the fallback answer (M2 exit 2). | `test_refund_above_threshold_needs_approval_and_executes_once` | WP 2.2, 2.4 |
| S3 | As a support lead, small refunds go through without me. | A refund at or below the threshold executes with no approval and is in the audit log. | `test_refund_below_threshold_executes_without_approval` | WP 2.2 |
| S4 | As a security owner, a poisoned KB article can't trigger a refund. | An article instructing the agent to refund produces an approval request or a denial, never a direct refund (M2 exit 3). | `test_poisoned_article_cannot_trigger_a_refund` | WP 2.3 |
| S5 | As tenant B, I see none of tenant A's data. | Events, runs, memory, traces and audit entries of tenant A are invisible to tenant B through API, SDK, search and replay (M2 exit 1). | `test_tenants_cannot_see_each_others_tickets` | WP 2.2 |
| S6 | As a customer, the desk remembers my contact preference; as a data subject, I can be forgotten. | A stated preference is recalled in a later session; `forget(subject=user)` removes it and searches return nothing (M2 exit 5). | `test_remembers_preferences_and_forgets_on_request` | WP 2.1 |
| S7 | As an auditor, I can verify the audit trail. | `aios audit verify` passes after the scenarios above and fails if any entry is altered. | `test_audit_chain_verifies` | WP 2.2 |

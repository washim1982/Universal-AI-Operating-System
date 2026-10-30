# Reference app: AI Support Desk

**Proves (M2 "Safe by Default", the MVP):** memory, write tools, approvals, injection defenses
and multi-tenancy. Built in WP 2.7; specified here in Phase 0 (WP 0.5).

Manifests: [manifests/](manifests/). Config schema: [config.schema.json](config.schema.json).
Draft policy: [policies/support-desk-policy.yaml](policies/support-desk-policy.yaml) (format
from requirements §7.9; validated once WP 2.2 compiles policies). Acceptance tests:
[tests/e2e/test_ai_support_desk.py](../../tests/e2e/test_ai_support_desk.py), marked
expected-to-fail until their work packages land.

## Shape

`handle-ticket` workflow, triggered by `zendesk.ticket.created` or by chat:

```
triage (support.triage) → research (support.kb.search) → resolve (support.resolve) → reply (support.reply)
```

`refund-agent` provides `support.resolve` and may call `stripe.refund.create`. The approval gate
is not a workflow step: the policy requires approval for refunds above the App's configured
threshold, and for any `financial` tool while the run context is tainted by untrusted content
(WP 2.3). Conditional routing (`when:` on steps) arrives with WP 1.3.

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

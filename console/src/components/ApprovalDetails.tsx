import Link from "next/link";

import { json, person, when } from "@/lib/format";
import type { Approval } from "@/lib/types";

import { RiskBadge, TaintNotice } from "./Badges";

/** Everything an approver needs (FR-HUM-03). Any of it can come from an attacker by way of what
 * the agent read, so it is all shown as plain text, and nothing here is ever rendered as HTML. */
export function ApprovalDetails({ approval }: { approval: Approval }) {
  const quorum = approval.quorum ?? 1;
  const votes = approval.votes ?? [];
  return (
    <article className="stack">
      <h1>{approval.summary}</h1>
      <p className="card-meta">
        <RiskBadge risk={approval.risk} />
        <span className="badge muted">{approval.status}</span>
        <span>{approval.data_class} data</span>
      </p>
      {approval.tainted && (
        <TaintNotice sources={approval.taint}>
          <strong>The agent read untrusted content before asking.</strong> It may be acting on
          instructions from someone else, so check what this does, not what it says.
        </TaintNotice>
      )}
      <section>
        <h2>Exactly what would happen</h2>
        {approval.action && (
          <p>
            <code>{approval.action}</code>
          </p>
        )}
        <pre>{json(approval.payload)}</pre>
      </section>
      {approval.preview !== undefined && approval.preview !== null && (
        <section>
          <h2>What the tool says it would do</h2>
          <pre>{typeof approval.preview === "string" ? approval.preview : json(approval.preview)}</pre>
          <p className="hint">A dry run by the tool itself. Nothing has happened yet.</p>
        </section>
      )}
      {approval.edits && (
        <section>
          <h2>Changed before it was approved</h2>
          <pre>{json(approval.edits)}</pre>
        </section>
      )}
      {quorum > 1 && (
        <section>
          <h2>
            {votes.length} of {quorum} approvals
          </h2>
          {votes.length > 0 ? (
            <ul>
              {votes.map((vote) => (
                <li key={vote.by}>
                  {person(vote.by)}
                  {vote.comment ? `: ${vote.comment}` : ""}
                </li>
              ))}
            </ul>
          ) : (
            <p className="hint">Nobody has said yes yet. One no is enough to stop it.</p>
          )}
        </section>
      )}
      {approval.rationale && (
        <section>
          <h2>Why the agent says it wants this</h2>
          <blockquote>{approval.rationale}</blockquote>
          <p className="hint">The agent&apos;s own words. A claim, not a fact.</p>
        </section>
      )}
      <section>
        <h2>Where this came from</h2>
        <dl>
          <dt>App</dt>
          <dd>{approval.app}</dd>
          <dt>Asked by</dt>
          <dd>{approval.requested_by}</dd>
          <dt>On behalf of</dt>
          <dd>{person(approval.initiator)}</dd>
          <dt>Rules that asked for this</dt>
          <dd>{approval.policies.join(", ") || "none named"}</dd>
          {approval.approvers && (
            <>
              <dt>Who can answer</dt>
              <dd>
                {approval.approvers.join(", ")}
                {(approval.level ?? 0) > 0 && " (escalated: nobody answered in time)"}
              </dd>
            </>
          )}
          <dt>Expires</dt>
          <dd>{when(approval.expires_at)} UTC. If nobody answers, the answer is no.</dd>
          {approval.run && (
            <>
              <dt>Run</dt>
              <dd>
                <Link href={`/runs/${encodeURIComponent(approval.app)}/${encodeURIComponent(approval.run)}`}>
                  {approval.run}
                </Link>
              </dd>
            </>
          )}
        </dl>
      </section>
    </article>
  );
}

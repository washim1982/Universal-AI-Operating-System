import Link from "next/link";

import { person, when } from "@/lib/format";
import type { Approval } from "@/lib/types";

import { RiskBadge } from "./Badges";

export function ApprovalList({
  approvals,
  empty = "Nothing is waiting for you.",
}: {
  approvals: Approval[];
  empty?: string;
}) {
  if (approvals.length === 0) {
    return <p className="empty">{empty}</p>;
  }
  return (
    <ul className="cards">
      {approvals.map((item) => (
        <li key={item.approval}>
          <Link className="card" href={`/approvals/${encodeURIComponent(item.approval)}`}>
            <span className="card-title">{item.summary}</span>
            <span className="card-meta">
              <RiskBadge risk={item.risk} />
              {item.tainted && <span className="badge warning">untrusted content</span>}
              <span>{item.app}</span>
              <span>for {person(item.initiator)}</span>
              <span>expires {when(item.expires_at)} UTC</span>
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

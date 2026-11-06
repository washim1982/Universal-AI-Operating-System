import type { ReactNode } from "react";

import { source } from "@/lib/format";
import type { Approval, RunStatus } from "@/lib/types";

const STATUS_TONE: Record<RunStatus, string> = {
  running: "info",
  paused: "warning",
  completed: "good",
  failed: "danger",
  cancelled: "muted",
};

export function StatusBadge({ status }: { status: RunStatus }) {
  return <span className={`badge ${STATUS_TONE[status]}`}>{status}</span>;
}

const RISK_TONE: Record<Approval["risk"], string> = {
  low: "muted",
  medium: "info",
  high: "warning",
  critical: "danger",
};

export function RiskBadge({ risk }: { risk: Approval["risk"] }) {
  return <span className={`badge ${RISK_TONE[risk]}`}>{risk} risk</span>;
}

/** Says plainly that outside content got in, and where it came from. */
export function TaintNotice({ sources, children }: { sources: string[]; children: ReactNode }) {
  return (
    <div className="notice warning" role="note">
      {children}
      {sources.length > 0 && (
        <ul>
          {sources.map((name) => (
            <li key={name}>{source(name)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

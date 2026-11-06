import Link from "next/link";

import { person, usd, when } from "@/lib/format";
import type { Run } from "@/lib/types";

import { StatusBadge } from "./Badges";

export function RunTable({ runs }: { runs: Run[] }) {
  if (runs.length === 0) {
    return <p className="empty">No runs yet.</p>;
  }
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">Status</th>
            <th scope="col">App</th>
            <th scope="col">Workflow</th>
            <th scope="col">For</th>
            <th scope="col">Started (UTC)</th>
            <th scope="col" className="number">
              Cost
            </th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.run_id}>
              <td>
                <Link href={`/runs/${encodeURIComponent(run.app)}/${encodeURIComponent(run.run_id)}`}>
                  <StatusBadge status={run.status} />
                </Link>
                {run.tainted && (
                  <span className="badge warning" title="This run read untrusted content">
                    untrusted
                  </span>
                )}
                {run.mode && (
                  <span className="badge info" title={`A ${run.mode} of ${run.source_run ?? "another run"}`}>
                    {run.mode}
                  </span>
                )}
              </td>
              <td>{run.app}</td>
              <td>{run.workflow}</td>
              <td>{person(run.on_behalf_of)}</td>
              <td>{when(run.started_at)}</td>
              <td className="number">{usd(run.cost_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

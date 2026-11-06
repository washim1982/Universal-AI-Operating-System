import type { Metadata } from "next";

import { StopForm } from "@/components/StopForm";
import { agents, isForbidden, suspensions, unless403 } from "@/lib/api";
import { when } from "@/lib/format";
import type { Agent } from "@/lib/types";

export const metadata: Metadata = { title: "Agents" };

function byApp(found: Agent[]): Map<string, Agent[]> {
  const groups = new Map<string, Agent[]>();
  for (const agent of found) groups.set(agent.app, [...(groups.get(agent.app) ?? []), agent]);
  return groups;
}

export default async function AgentsPage() {
  const [found, stopped] = await Promise.all([agents(), unless403(suspensions)]);
  const control = !isForbidden(stopped); // only someone who can use the kill switch sees it
  const appStopped = new Set(
    isForbidden(stopped) ? [] : stopped.filter((s) => s.kind === "app").map((s) => s.name),
  );
  const agentStopped = new Set(
    isForbidden(stopped) ? [] : stopped.filter((s) => s.kind === "agent").map((s) => s.name),
  );
  const groups = byApp(found);
  return (
    <div className="stack">
      <h1>Agents</h1>
      {groups.size === 0 && <p className="empty">No agents are installed.</p>}
      {[...groups].map(([app, list]) => (
        <section key={app} className="stack">
          <div className="row spread">
            <h2>{app}</h2>
            {control && <StopForm kind="app" name={app} stopped={appStopped.has(app)} />}
          </div>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Agent</th>
                  <th scope="col">Status</th>
                  <th scope="col">Does</th>
                  <th scope="col">May use</th>
                  <th scope="col" className="number">Done</th>
                  <th scope="col" className="number">Failed</th>
                  <th scope="col">Last active (UTC)</th>
                  {control && <th scope="col" />}
                </tr>
              </thead>
              <tbody>
                {list.map((agent) => (
                  <tr key={agent.agent}>
                    <td>
                      {agent.agent} <small>{agent.version}</small>
                    </td>
                    <td>
                      {agent.suspended ? (
                        <span className="badge danger" title={agent.suspension ?? undefined}>
                          stopped
                        </span>
                      ) : (
                        <span className="badge good">working</span>
                      )}
                      {agent.suspension && <div className="hint">{agent.suspension}</div>}
                    </td>
                    <td>{agent.capabilities.join(", ")}</td>
                    <td>
                      {agent.tools.filter((t) => !agent.refused_tools.includes(t)).join(", ") || "no tools"}
                      {agent.refused_tools.length > 0 && (
                        <div className="hint">refused when installed: {agent.refused_tools.join(", ")}</div>
                      )}
                      {(agent.memory_read.length > 0 || agent.memory_write.length > 0) && (
                        <div className="hint">
                          memory: reads {agent.memory_read.join(", ") || "nothing"}, writes{" "}
                          {agent.memory_write.join(", ") || "nothing"}
                        </div>
                      )}
                    </td>
                    <td className="number">{agent.completed}</td>
                    <td className="number">{agent.failed}</td>
                    <td>{when(agent.last_active)}</td>
                    {control && (
                      <td>
                        <StopForm kind="agent" name={agent.agent} stopped={agentStopped.has(agent.agent)} />
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="hint">Done and failed count the App&apos;s latest 200 task events.</p>
        </section>
      ))}
    </div>
  );
}

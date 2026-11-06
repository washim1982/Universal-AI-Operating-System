import type { Metadata } from "next";

import { Forbidden } from "@/components/Forbidden";
import { Simulator } from "@/components/Simulator";
import { agents, apps, isForbidden, policies, unless403 } from "@/lib/api";
import { json } from "@/lib/format";

import { ask } from "./actions";

export const metadata: Metadata = { title: "Policies" };

export default async function PoliciesPage({
  searchParams,
}: {
  searchParams: Promise<{ app?: string }>;
}) {
  const installed = await apps();
  const wanted = (await searchParams).app;
  const app = installed.find((item) => item.app === wanted)?.app ?? installed[0]?.app;
  if (!app) return <p className="empty">No Apps are installed.</p>;
  const [view, everyone] = await Promise.all([unless403(() => policies(app)), agents()]);
  if (isForbidden(view)) return <Forbidden what="reading policies" detail={view.forbidden} />;
  const names = everyone.filter((agent) => agent.app === app).map((agent) => agent.agent);
  return (
    <div className="stack">
      <h1>Policies</h1>
      <form className="row filters" aria-label="App">
        <label>
          App
          <select name="app" defaultValue={app}>
            {installed.map((item) => (
              <option key={item.app}>{item.app}</option>
            ))}
          </select>
        </label>
        <button className="button" type="submit">
          Show
        </button>
      </form>
      <section className="stack">
        <h2>Would this be allowed?</h2>
        {names.length === 0 ? (
          <p className="empty">{app} has no agents to ask about.</p>
        ) : (
          <Simulator ask={ask.bind(null, app)} agents={names} />
        )}
      </section>
      <section>
        <h2>What decides</h2>
        <p className="hint">
          The built-in roles come first, then these, in order. A deny anywhere wins over any allow.
        </p>
        <ul className="plain">
          {view.policies.map((policy) => (
            <li key={policy.metadata.name}>
              <details>
                <summary>
                  <code>{policy.metadata.name}</code> <small>{policy.metadata.version}</small>
                </summary>
                <pre>{json(policy.spec)}</pre>
              </details>
            </li>
          ))}
        </ul>
      </section>
      <section>
        <h2>Rules</h2>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Rule</th>
                <th scope="col">Kind</th>
                <th scope="col">Who approves</th>
              </tr>
            </thead>
            <tbody>
              {view.rules.map((rule) => (
                <tr key={rule.id}>
                  <td>
                    <code>{rule.id}</code>
                  </td>
                  <td>{rule.kind}</td>
                  <td>{rule.approvers.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <details>
          <summary>As Cedar runs it</summary>
          <pre>{view.cedar}</pre>
        </details>
      </section>
    </div>
  );
}

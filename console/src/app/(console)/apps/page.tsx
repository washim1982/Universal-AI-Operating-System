import type { Metadata } from "next";
import Link from "next/link";

import { InstallBadge } from "@/components/InstallBadge";
import { apps, installations, isForbidden, unless403 } from "@/lib/api";
import { person, when } from "@/lib/format";

export const metadata: Metadata = { title: "Apps" };

/** Apps a server was started with (``--app``) run, but nobody installed them, so nobody agreed. */
function Started({ names }: { names: string[] }) {
  if (names.length === 0) return null;
  return (
    <section>
      <h2>Started with the server</h2>
      <p className="hint">These were given to AIOS when it started, so they have no consent step.</p>
      <ul className="plain">
        {names.map((name) => (
          <li key={name}>{name}</li>
        ))}
      </ul>
    </section>
  );
}

export default async function AppsPage() {
  const [found, running] = await Promise.all([unless403(installations), apps()]);
  if (isForbidden(found)) {
    return (
      <div className="stack">
        <h1>Apps</h1>
        <p className="hint">Installing and configuring Apps needs a tenant admin. These are running:</p>
        <ul className="plain">
          {running.map((app) => (
            <li key={app.app}>
              {app.app} <small>{app.version}</small>
            </li>
          ))}
        </ul>
      </div>
    );
  }
  return (
    <div className="stack">
      <h1>Apps</h1>
      {found.length === 0 ? (
        <p className="empty">
          Nothing is installed yet. Offer an App with <code>aios app install</code>.
        </p>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">App</th>
                <th scope="col">Status</th>
                <th scope="col">Running</th>
                <th scope="col">Waiting</th>
                <th scope="col">Agreed by</th>
                <th scope="col">Changed (UTC)</th>
              </tr>
            </thead>
            <tbody>
              {found.map((item) => (
                <tr key={item.app}>
                  <td>
                    <Link href={`/apps/${encodeURIComponent(item.app)}`}>{item.app}</Link>
                  </td>
                  <td>
                    <InstallBadge status={item.status} />
                  </td>
                  <td>{item.version}</td>
                  <td>{item.pending && <span className="badge warning">{item.pending}</span>}</td>
                  <td>{item.granted_by && person(item.granted_by)}</td>
                  <td>{when(item.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Started names={running.map((app) => app.app).filter((name) => !found.some((i) => i.app === name))} />
    </div>
  );
}

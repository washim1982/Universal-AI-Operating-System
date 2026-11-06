import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { ActionForm } from "@/components/ActionForm";
import { ConsentForm } from "@/components/ConsentForm";
import { Forbidden } from "@/components/Forbidden";
import { InstallBadge } from "@/components/InstallBadge";
import { ApiError, installation, isForbidden, unless403, versions } from "@/lib/api";
import { json, person, when } from "@/lib/format";
import type { Grants } from "@/lib/types";

import { remove, rollBack, saveConfig } from "../actions";

export const metadata: Metadata = { title: "App" };

function GrantList({ grants, empty }: { grants: Grants; empty: string }) {
  const all = [...grants.tools, ...grants.data_classes, ...grants.memory_scopes, ...grants.hosts];
  if (all.length === 0) return <p className="empty">{empty}</p>;
  return (
    <ul className="plain tags">
      {all.map((value) => (
        <li key={value}>
          <code>{value}</code>
        </li>
      ))}
    </ul>
  );
}

export default async function AppPage({ params }: { params: Promise<{ app: string }> }) {
  const { app } = await params;
  const found = await unless403(() => installation(app)).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  });
  if (isForbidden(found)) {
    return <Forbidden what="managing Apps" detail={found.forbidden} />;
  }
  const history = await versions(app);
  return (
    <div className="stack">
      <Link href="/apps">← Apps</Link>
      <h1>
        {found.app} {found.version && <small>{found.version}</small>}
      </h1>
      <p className="card-meta">
        <InstallBadge status={found.status} />
        {found.granted_by && <span>agreed by {person(found.granted_by)}</span>}
        <span>changed {when(found.updated_at)} UTC</span>
      </p>
      {found.pending && found.requested && (
        <ConsentForm
          app={found.app}
          version={found.pending}
          requested={found.requested}
          refused={found.refused}
        />
      )}
      <section>
        <h2>Granted</h2>
        <GrantList grants={found.grants} empty="Nothing is granted." />
        <h2>Refused</h2>
        <GrantList grants={found.refused} empty="Nothing has been refused." />
      </section>
      {found.status === "active" && (
        <section className="stack">
          <h2>Configuration</h2>
          <ActionForm action={saveConfig.bind(null, found.app)} className="stack" label="Configuration">
            <label>
              JSON, checked against the App&apos;s schema
              <textarea name="config" rows={6} defaultValue={json(found.config)} spellCheck={false} />
            </label>
            <div className="row">
              <button className="button" type="submit">
                Save configuration
              </button>
            </div>
          </ActionForm>
        </section>
      )}
      <section>
        <h2>Versions</h2>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Version</th>
                <th scope="col">Digest</th>
                <th scope="col">Asks for</th>
                <th scope="col" />
              </tr>
            </thead>
            <tbody>
              {history.map((item) => (
                <tr key={item.version}>
                  <td>
                    {item.version} {item.running && <span className="badge good">running</span>}
                    {item.pending && <span className="badge warning">waiting</span>}
                  </td>
                  <td>
                    <code title={item.digest}>{item.digest.slice(7, 19)}</code>
                  </td>
                  <td>{item.requested.tools.join(", ") || "no tools"}</td>
                  <td>
                    {!item.running && found.status === "active" && (
                      <ActionForm action={rollBack.bind(null, found.app)} label={`Roll back to ${item.version}`}>
                        <input type="hidden" name="version" value={item.version} />
                        <button className="button" type="submit">
                          Go back to this
                        </button>
                      </ActionForm>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="stack">
        <h2>Its data</h2>
        <p>
          <a className="button" href={`/apps/${encodeURIComponent(found.app)}/export`} download>
            Export everything
          </a>{" "}
          <span className="hint">Its events and memory, one JSON object a line.</span>
        </p>
      </section>
      {found.status !== "uninstalled" && (
        <section className="stack">
          <h2>Uninstall</h2>
          <ActionForm action={remove.bind(null, found.app)} className="stack" label="Uninstall">
            <fieldset>
              <legend>What happens to its data</legend>
              <label className="check">
                <input type="radio" name="data" value="retain" required /> Keep it
              </label>
              <label className="check">
                <input type="radio" name="data" value="delete" /> Delete its memory (export it
                first if you want a copy)
              </label>
            </fieldset>
            <label>
              Type <code>{found.app}</code> to confirm
              <input name="confirm" autoComplete="off" required />
            </label>
            <div className="row">
              <button className="button danger" type="submit">
                Uninstall
              </button>
            </div>
          </ActionForm>
        </section>
      )}
    </div>
  );
}

import type { Metadata } from "next";
import Link from "next/link";

import { Forbidden } from "@/components/Forbidden";
import { isForbidden, searchMemory, unless403 } from "@/lib/api";
import { when } from "@/lib/format";

export const metadata: Metadata = { title: "Memory" };

const SCOPES = ["app", "tenant", "user", "agent", "session", "global"];
const DEFAULT = ["app", "tenant", "user"];

export default async function MemoryPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string; scope?: string | string[] }>;
}) {
  const params = await searchParams;
  const wanted = (params.q ?? "").trim();
  const asked = params.scope === undefined ? DEFAULT : [params.scope].flat();
  const scopes = asked.filter((scope) => SCOPES.includes(scope));
  const found = wanted && scopes.length > 0 ? await unless403(() => searchMemory(wanted, scopes)) : [];
  if (isForbidden(found)) return <Forbidden what="managing memory" detail={found.forbidden} />;
  return (
    <div className="stack">
      <h1>Memory</h1>
      <form className="stack filters" role="search" aria-label="Search memory">
        <div className="row">
          <label>
            Words to find
            <input name="q" defaultValue={wanted} required />
          </label>
          <button className="button" type="submit">
            Search
          </button>
        </div>
        <fieldset className="row">
          <legend>Scopes</legend>
          {SCOPES.map((scope) => (
            <label key={scope} className="check">
              <input type="checkbox" name="scope" value={scope} defaultChecked={scopes.includes(scope)} />
              {scope}
            </label>
          ))}
        </fieldset>
      </form>
      <p className="hint">Every search here is on the audit record, like an agent&apos;s.</p>
      {wanted && found.length === 0 && <p className="empty">Nothing matches.</p>}
      {found.length > 0 && (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Item</th>
                <th scope="col">Scope</th>
                <th scope="col">Label</th>
                <th scope="col">From</th>
                <th scope="col">Changed (UTC)</th>
              </tr>
            </thead>
            <tbody>
              {found.map((item) => (
                <tr key={item.id}>
                  <td>
                    <Link href={`/memory/${encodeURIComponent(item.id)}`}>
                      {item.content.length > 80 ? `${item.content.slice(0, 80)}…` : item.content}
                    </Link>
                  </td>
                  <td>
                    {item.scope} <small>{item.owner}</small>
                  </td>
                  <td>{item.sensitivity}</td>
                  <td>{item.provenance.source_uri ?? item.document ?? ""}</td>
                  <td>{when(item.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

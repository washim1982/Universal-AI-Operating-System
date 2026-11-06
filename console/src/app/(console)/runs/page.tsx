import type { Metadata } from "next";

import { RunTable } from "@/components/RunTable";
import { ApiError, apps, runs } from "@/lib/api";
import { filterFrom, matches, STATUSES } from "@/lib/search";
import type { Run } from "@/lib/types";

export const metadata: Metadata = { title: "Runs" };

/** A caller may see some Apps and not others, and that isn't a failure of the page. */
async function runsOf(app: string): Promise<Run[]> {
  try {
    return await runs(app);
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) return [];
    throw error;
  }
}

export default async function RunsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const filter = filterFrom(await searchParams);
  const installed = await apps();
  const chosen = filter.app ? installed.filter(({ app }) => app === filter.app) : installed;
  const lists = await Promise.all(chosen.map(({ app }) => runsOf(app)));
  const found = lists
    .flat()
    .filter((run) => matches(run, filter))
    .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""));
  return (
    <div className="stack">
      <h1>Runs</h1>
      <form className="row filters" role="search" aria-label="Search runs">
        <label>
          App
          <select name="app" defaultValue={filter.app ?? ""}>
            <option value="">All</option>
            {installed.map(({ app }) => (
              <option key={app} value={app}>
                {app}
              </option>
            ))}
          </select>
        </label>
        <label>
          Status
          <select name="status" defaultValue={filter.status ?? ""}>
            <option value="">Any</option>
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </select>
        </label>
        <label>
          Run, workflow or person
          <input name="q" defaultValue={filter.text ?? ""} />
        </label>
        <label className="check">
          <input type="checkbox" name="untrusted" value="1" defaultChecked={filter.untrusted} />
          Read untrusted content
        </label>
        <button className="button" type="submit">
          Search
        </button>
      </form>
      <p className="hint">The newest 200 runs of each App are searched.</p>
      <RunTable runs={found} />
    </div>
  );
}

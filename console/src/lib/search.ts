/** Run search (FR-UI-03). The API lists each App's newest runs; the console narrows them down. */

import type { Run, RunStatus } from "./types";

export interface RunFilter {
  app?: string;
  status?: RunStatus | "";
  text?: string;
  untrusted?: boolean;
}

const STATUSES: RunStatus[] = ["running", "paused", "completed", "failed", "cancelled"];

/** A filter from the page's address, keeping only what makes sense. */
export function filterFrom(params: Record<string, string | string[] | undefined>): RunFilter {
  const one = (name: string) => {
    const value = params[name];
    return (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
  };
  const status = one("status");
  return {
    app: one("app") || undefined,
    status: (STATUSES as string[]).includes(status) ? (status as RunStatus) : "",
    text: one("q") || undefined,
    untrusted: one("untrusted") === "1",
  };
}

export function matches(run: Run, filter: RunFilter): boolean {
  if (filter.app && run.app !== filter.app) return false;
  if (filter.status && run.status !== filter.status) return false;
  if (filter.untrusted && !run.tainted) return false;
  if (filter.text) {
    const wanted = filter.text.toLowerCase();
    const haystack = [run.run_id, run.workflow ?? "", run.on_behalf_of ?? ""].join(" ").toLowerCase();
    if (!haystack.includes(wanted)) return false;
  }
  return true;
}

export { STATUSES };

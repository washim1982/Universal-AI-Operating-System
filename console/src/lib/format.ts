/** How things read on the page. */

export function when(iso: string | null): string {
  if (iso === null) return "";
  return new Date(iso).toLocaleString("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  });
}

export function usd(amount: number): string {
  return amount === 0 ? "$0" : `$${amount.toFixed(4)}`;
}

/** ``user:ada@acme`` reads as ``ada``; anything else is shown as it is. */
export function person(principal: string | null): string {
  if (principal === null) return "nobody";
  const match = /^(?:user|service):([^@]+)@/.exec(principal);
  return match?.[1] ?? principal;
}

/** Where untrusted content came from, in words. The raw name stays beside it. */
export function source(name: string): string {
  if (name === "input") return "the request";
  if (name.startsWith("tool:")) return `the tool ${name.slice(5)}`;
  if (name.startsWith("memory:")) return `a stored document (${name.slice(7)})`;
  if (name.startsWith("suspected:")) return `what looks like an injection, in ${source(name.slice(10))}`;
  return name;
}

export function json(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "";
}

/** A duration a person can read: ``850 ms``, ``2.4 s``, ``3 min 5 s``. */
export function duration(ms: number | null): string {
  if (ms === null) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes} min ${Math.round((ms % 60_000) / 1000)} s`;
}

export function count(value: number): string {
  return value.toLocaleString("en-GB");
}

/** How far through a budget, from 0 to 100, never more: a bar can't go past its end. */
export function share(spent: number, limit: number): number {
  if (limit <= 0) return 100;
  return Math.min(100, Math.round((spent / limit) * 100));
}

/** This month, as the API names it. */
export function thisMonth(now: Date = new Date()): string {
  return now.toISOString().slice(0, 7);
}

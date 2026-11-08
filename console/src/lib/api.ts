/** Every call the console makes to AIOS. It runs on the server, with the caller's own credential,
 * so what someone sees and can do here is what the API lets them see and do. */

import { redirect } from "next/navigation";

import { server } from "./config";
import { session } from "./session";
import type {
  Agent,
  App,
  Approval,
  ApprovalStatus,
  Comparison,
  Contact,
  CostReport,
  DeadLetter,
  Feedback,
  Grants,
  HumanTask,
  Installation,
  Me,
  MemoryItem,
  PolicyView,
  RestartMode,
  Run,
  RunDetails,
  RunEvent,
  SimulatedAction,
  StoredEvent,
  Suspension,
  Trace,
  Verdict,
  Version,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** A page this caller's roles don't reach. Shown as such, not as a failure. */
export interface Forbidden {
  forbidden: string;
}

export function isForbidden(value: unknown): value is Forbidden {
  return typeof value === "object" && value !== null && "forbidden" in value;
}

/** ``read()``, or what the API said about why not, when that was a 403. */
export async function unless403<T>(read: () => Promise<T>): Promise<T | Forbidden> {
  try {
    return await read();
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) return { forbidden: error.message };
    throw error;
  }
}

async function detail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null && "detail" in body) {
      const said = body.detail;
      return typeof said === "string" ? said : JSON.stringify(said);
    }
  } catch {
    // Not JSON: the status says enough.
  }
  return `the server answered ${response.status}`;
}

async function headers(init: RequestInit): Promise<Headers> {
  const { token } = await session();
  const found = new Headers(init.headers);
  if (token) found.set("Authorization", `Bearer ${token}`);
  return found;
}

/** The raw response, for a download. A 401 still means signing in again. */
export async function fetchRaw(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(`${server}${path}`, {
    ...init,
    headers: await headers(init),
    cache: "no-store",
  });
  if (response.status === 401) redirect("/login");
  if (!response.ok) throw new ApiError(response.status, await detail(response));
  return response;
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetchRaw(path, init);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  return call(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

const part = encodeURIComponent;

function query(values: Record<string, string | number | boolean | undefined>): string {
  const found = new URLSearchParams();
  for (const [name, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") found.set(name, String(value));
  }
  const text = found.toString();
  return text ? `?${text}` : "";
}

export function me(): Promise<Me> {
  return call("/v1/me");
}

export function apps(): Promise<App[]> {
  return call("/v1/apps");
}

// --------------------------------------------------------------------------- runs

export function runs(app: string): Promise<Run[]> {
  return call(`/v1/apps/${part(app)}/runs?limit=200`);
}

export function run(app: string, id: string): Promise<RunDetails> {
  return call(`/v1/apps/${part(app)}/runs/${part(id)}`);
}

export function runEvents(app: string, id: string): Promise<RunEvent[]> {
  return call(`/v1/apps/${part(app)}/runs/${part(id)}/events`);
}

export function trace(app: string, id: string): Promise<Trace> {
  return call(`/v1/apps/${part(app)}/runs/${part(id)}/trace`);
}

export function controlRun(
  app: string,
  id: string,
  action: "pause" | "resume" | "cancel",
): Promise<unknown> {
  return send("POST", `/v1/apps/${part(app)}/runs/${part(id)}/${action}`);
}

/** What a fork or retry changes. What is left out stays as it was in the source. */
export interface Changes {
  step?: string;
  input?: unknown;
  step_input?: unknown;
  model?: string;
}

/** Start a replay, fork or retry of a finished run. AIOS answers with the new run. */
export function restartRun(
  app: string,
  id: string,
  mode: RestartMode,
  changes: Changes = {},
): Promise<{ run_id: string }> {
  return send("POST", `/v1/apps/${part(app)}/runs/${part(id)}/${mode}`, changes);
}

export function compareRuns(app: string, a: string, b: string): Promise<Comparison> {
  return call(`/v1/apps/${part(app)}/runs/${part(a)}/compare/${part(b)}`);
}

// --------------------------------------------------------------------------- approvals

export function inbox(status: ApprovalStatus = "pending", everyone = false): Promise<Approval[]> {
  return call(`/v1/approvals${query({ status, everyone: everyone || undefined })}`);
}

export function approval(id: string): Promise<Approval> {
  return call(`/v1/approvals/${part(id)}`);
}

export function answer(
  id: string,
  verdict: "approve" | "deny",
  comment?: string,
  edits?: Record<string, unknown>,
): Promise<Approval> {
  const body: Record<string, unknown> = comment ? { comment } : {};
  if (edits) body.edits = edits;
  return send("POST", `/v1/approvals/${part(id)}/${verdict}`, body);
}

// --------------------------------------------------------------------------- work for people

export function tasks(everyone = false): Promise<HumanTask[]> {
  return call(`/v1/tasks${query({ everyone: everyone || undefined })}`);
}

export function task(id: string): Promise<HumanTask> {
  return call(`/v1/tasks/${part(id)}`);
}

export function completeTask(id: string, output: unknown, comment?: string): Promise<HumanTask> {
  return send("POST", `/v1/tasks/${part(id)}/complete`, { output, comment: comment ?? null });
}

export function contact(): Promise<Contact> {
  return call("/v1/me/contact");
}

export function setContact(found: Contact): Promise<Contact> {
  return send("PUT", "/v1/me/contact", found);
}

export function giveFeedback(
  app: string,
  id: string,
  body: Partial<Pick<Feedback, "step" | "rating" | "correction" | "comment" | "remember">>,
): Promise<Feedback> {
  return send("POST", `/v1/apps/${part(app)}/runs/${part(id)}/feedback`, body);
}

export function feedbackOn(app: string, id: string): Promise<Feedback[]> {
  return call(`/v1/apps/${part(app)}/feedback${query({ run: id })}`);
}

// --------------------------------------------------------------------------- apps

export function installations(): Promise<Installation[]> {
  return call("/v1/installations");
}

export function installation(app: string): Promise<Installation> {
  return call(`/v1/installations/${part(app)}`);
}

export function versions(app: string): Promise<Version[]> {
  return call(`/v1/installations/${part(app)}/versions`);
}

export function consent(app: string, grants?: Grants): Promise<Installation> {
  return send("POST", `/v1/installations/${part(app)}/consent`, grants ? { grants } : {});
}

export function refuse(app: string): Promise<Installation> {
  return send("POST", `/v1/installations/${part(app)}/refuse`);
}

export function rollback(app: string, version: string): Promise<Installation> {
  return send("POST", `/v1/installations/${part(app)}/rollback`, { version });
}

export function configure(app: string, config: Record<string, unknown>): Promise<Installation> {
  return send("PUT", `/v1/installations/${part(app)}/config`, config);
}

export function uninstall(app: string, data: "retain" | "delete"): Promise<Installation> {
  return send("DELETE", `/v1/installations/${part(app)}${query({ data })}`);
}

export function exported(app: string): Promise<Response> {
  return fetchRaw(`/v1/installations/${part(app)}/export`);
}

// --------------------------------------------------------------------------- agents

export function agents(): Promise<Agent[]> {
  return call("/v1/agents");
}

export function suspensions(): Promise<Suspension[]> {
  return call("/v1/suspensions");
}

export function suspend(kind: "agent" | "app", name: string, reason: string): Promise<unknown> {
  return send("POST", "/v1/suspensions", { kind, name, reason });
}

export function lift(kind: "agent" | "app", name: string): Promise<unknown> {
  return send("DELETE", `/v1/suspensions/${part(kind)}/${part(name)}`);
}

// --------------------------------------------------------------------------- costs

export function costs(month?: string): Promise<CostReport> {
  return call(`/v1/costs${query({ month })}`);
}

// --------------------------------------------------------------------------- policies

export function policies(app: string): Promise<PolicyView> {
  return call(`/v1/apps/${part(app)}/policies`);
}

export interface Question {
  action: SimulatedAction;
  agent: string;
  resource: string;
  side_effect?: string;
  on_behalf_of?: string;
  tainted: boolean;
  approved: boolean;
  input: Record<string, unknown>;
}

export function simulate(app: string, question: Question): Promise<Verdict> {
  return send("POST", `/v1/apps/${part(app)}/policies/simulate`, question);
}

// --------------------------------------------------------------------------- memory

export function searchMemory(text: string, scopes: string[]): Promise<MemoryItem[]> {
  return send("POST", "/v1/memory/search", { query: text, scopes, k: 50 });
}

export function memoryItem(id: string): Promise<MemoryItem> {
  return call(`/v1/memory/${part(id)}`);
}

export function correctMemory(id: string, content: string, version: number): Promise<MemoryItem> {
  return send("PUT", `/v1/memory/${part(id)}`, { content, version });
}

export function forgetMemory(id: string): Promise<{ erased: number; held: number }> {
  return send("POST", "/v1/memory/forget", { item: id });
}

// --------------------------------------------------------------------------- events

export function latestEvents(app?: string, pattern?: string, limit = 200): Promise<StoredEvent[]> {
  return call(`/v1/events${query({ app, pattern, limit, newest: true })}`);
}

export function deadLetters(): Promise<DeadLetter[]> {
  return call("/v1/dlq");
}

export function redrive(id: string): Promise<unknown> {
  return send("POST", `/v1/dlq/${part(id)}/redrive`);
}

export function discard(id: string): Promise<unknown> {
  return send("DELETE", `/v1/dlq/${part(id)}`);
}

/** Whether a credential works, without acting on it. */
export async function accepts(token: string): Promise<boolean> {
  const response = await fetch(`${server}/v1/me`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  return response.ok;
}

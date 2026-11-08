/**
 * The AIOS API, for TypeScript and JavaScript (FR-CP-04).
 *
 *     const aios = new AIOS({ server: "https://aios.example.com", token: process.env.AIOS_API_KEY });
 *     const run = await aios.ask("ai-researcher", "What changed in OpenTelemetry in 2026?");
 *     console.log(run.status, run.output);
 *
 * Everything goes through the API with the caller's own credential, so what a program can see and
 * do is what that credential may. Runs, approvals, tasks for people and feedback are here; so is
 * following a run's events as they happen.
 */

import type {
  AppDescription,
  AppSummary,
  Approval,
  ApprovalStatus,
  Feedback,
  FeedbackRequest,
  HumanTask,
  JsonValue,
  Me,
  Run,
  RunEvent,
  RunRequest,
  RunStarted,
  RunSummary,
  SchemaRegistry,
} from "./types.js";

export type * from "./types.js";

/** The events that end a run. */
export const TERMINAL: ReadonlySet<string> = new Set(["run.completed", "run.failed", "run.cancelled"]);

/** AIOS said no, or wasn't there. ``status`` is the HTTP status; 0 when nothing answered. */
export class AIOSError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "AIOSError";
  }
}

export interface ClientOptions {
  /** Where the AIOS API is, e.g. ``https://aios.example.com``. */
  server: string;
  /** An API key (``aios_...``) or an identity provider's token. A local ``aios dev`` needs none. */
  token?: string;
  /** For a platform administrator: the tenant to act in. */
  tenant?: string;
  /** A ``fetch`` of your own, e.g. for a proxy, or in tests. */
  fetch?: typeof fetch;
}

export interface FollowOptions {
  signal?: AbortSignal;
  /** Start after this event position (the ``id`` of a server-sent event). */
  after?: number;
}

type Query = Record<string, string | number | boolean | undefined>;

export class AIOS {
  private readonly server: string;
  private readonly headers: Record<string, string>;
  private readonly fetcher: typeof fetch;

  constructor(options: ClientOptions) {
    this.server = options.server.replace(/\/+$/, "");
    this.headers = {};
    if (options.token) this.headers.Authorization = `Bearer ${options.token}`;
    if (options.tenant) this.headers["X-AIOS-Tenant"] = options.tenant;
    this.fetcher = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  // ------------------------------------------------------------------ who, and which Apps

  me(): Promise<Me> {
    return this.call("GET", "/v1/me");
  }

  apps(): Promise<AppSummary[]> {
    return this.call("GET", "/v1/apps");
  }

  app(name: string): Promise<AppDescription> {
    return this.call("GET", `/v1/apps/${part(name)}`);
  }

  // ------------------------------------------------------------------ runs

  /** Start a run: a chat message, or the workflow's input. It returns at once. */
  startRun(app: string, request: string | RunRequest): Promise<RunStarted> {
    const body = typeof request === "string" ? { message: request } : request;
    return this.call("POST", `/v1/apps/${part(app)}/runs`, body);
  }

  run(app: string, runId: string): Promise<Run> {
    return this.call("GET", `/v1/apps/${part(app)}/runs/${part(runId)}`);
  }

  runs(app: string, options: { limit?: number } = {}): Promise<RunSummary[]> {
    return this.call("GET", `/v1/apps/${part(app)}/runs${query({ limit: options.limit })}`);
  }

  /** Every event of a run so far. */
  events(app: string, runId: string): Promise<RunEvent[]> {
    return this.call("GET", `/v1/apps/${part(app)}/runs/${part(runId)}/events`);
  }

  /** A run's events as they happen, until it ends. A dropped connection picks up where it was. */
  async *follow(app: string, runId: string, options: FollowOptions = {}): AsyncGenerator<RunEvent> {
    let after = options.after ?? 0;
    for (;;) {
      const response = await this.send(
        "GET",
        `/v1/apps/${part(app)}/runs/${part(runId)}/events`,
        undefined,
        { Accept: "text/event-stream", "Last-Event-ID": String(after) },
        options.signal,
      );
      if (!response.body) throw new AIOSError(response.status, "the event stream has no body");
      for await (const message of serverSentEvents(response.body)) {
        if (message.id !== undefined) after = Number(message.id);
        const event = JSON.parse(message.data) as RunEvent;
        yield event;
        if (TERMINAL.has(event.type)) return;
      }
      if (options.signal?.aborted) return;
    }
  }

  /** Start a run, wait for it to end, and return it. */
  async ask(app: string, request: string | RunRequest, options: FollowOptions = {}): Promise<Run> {
    const started = await this.startRun(app, request);
    for await (const event of this.follow(app, started.run_id, options)) {
      void event; // until the run ends
    }
    return this.run(app, started.run_id);
  }

  cancel(app: string, runId: string): Promise<unknown> {
    return this.call("POST", `/v1/apps/${part(app)}/runs/${part(runId)}/cancel`);
  }

  pause(app: string, runId: string): Promise<unknown> {
    return this.call("POST", `/v1/apps/${part(app)}/runs/${part(runId)}/pause`);
  }

  resume(app: string, runId: string): Promise<unknown> {
    return this.call("POST", `/v1/apps/${part(app)}/runs/${part(runId)}/resume`);
  }

  /** External input for a run, which a ``wait`` step takes. */
  signal(app: string, runId: string, name: string, data?: JsonValue): Promise<unknown> {
    return this.call("POST", `/v1/apps/${part(app)}/runs/${part(runId)}/signal`, { name, data: data ?? null });
  }

  // ------------------------------------------------------------------ approvals

  /** What waits for you; with ``everyone``, an auditor's view by status. */
  approvals(options: { status?: ApprovalStatus; everyone?: boolean } = {}): Promise<Approval[]> {
    return this.call("GET", `/v1/approvals${query({ status: options.status, everyone: options.everyone })}`);
  }

  approval(id: string): Promise<Approval> {
    return this.call("GET", `/v1/approvals/${part(id)}`);
  }

  /** Say yes. ``edits`` changes some of the action's values first; AIOS checks the change again. */
  approve(id: string, options: { comment?: string; edits?: Record<string, JsonValue> } = {}): Promise<Approval> {
    return this.call("POST", `/v1/approvals/${part(id)}/approve`, options);
  }

  deny(id: string, options: { comment?: string } = {}): Promise<Approval> {
    return this.call("POST", `/v1/approvals/${part(id)}/deny`, options);
  }

  // ------------------------------------------------------------------ work for people

  tasks(options: { everyone?: boolean } = {}): Promise<HumanTask[]> {
    return this.call("GET", `/v1/tasks${query({ everyone: options.everyone })}`);
  }

  task(id: string): Promise<HumanTask> {
    return this.call("GET", `/v1/tasks/${part(id)}`);
  }

  /** Do the work: ``output`` goes back to the run, and must fit what the task asks for. */
  completeTask(id: string, output: JsonValue, options: { comment?: string } = {}): Promise<HumanTask> {
    return this.call("POST", `/v1/tasks/${part(id)}/complete`, { output, comment: options.comment ?? null });
  }

  // ------------------------------------------------------------------ feedback

  feedback(app: string, runId: string, feedback: FeedbackRequest): Promise<Feedback> {
    return this.call("POST", `/v1/apps/${part(app)}/runs/${part(runId)}/feedback`, feedback);
  }

  // ------------------------------------------------------------------ schemas

  schemas(): Promise<SchemaRegistry> {
    return this.call("GET", "/v1/schemas");
  }

  /** An event type's payload, as a JSON Schema. */
  eventSchema(type: string): Promise<Record<string, JsonValue>> {
    return this.call("GET", `/v1/schemas/events/${part(type)}`);
  }

  // ------------------------------------------------------------------ the wire

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await this.send(method, path, body);
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  private async send(
    method: string,
    path: string,
    body?: unknown,
    headers: Record<string, string> = {},
    signal?: AbortSignal,
  ): Promise<Response> {
    const init: RequestInit = { method, headers: { ...this.headers, ...headers } };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      (init.headers as Record<string, string>)["Content-Type"] = "application/json";
    }
    if (signal) init.signal = signal;
    let response: Response;
    try {
      response = await this.fetcher(`${this.server}${path}`, init);
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") throw error;
      throw new AIOSError(0, `no answer from AIOS at ${this.server}: ${String(error)}`);
    }
    if (!response.ok) {
      const detail = await detailOf(response);
      throw new AIOSError(response.status, typeof detail === "string" ? detail : `AIOS answered ${response.status}`, detail);
    }
    return response;
  }
}

function part(value: string): string {
  return encodeURIComponent(value);
}

function query(values: Query): string {
  const found = new URLSearchParams();
  for (const [name, value] of Object.entries(values)) {
    if (value !== undefined && value !== false) found.set(name, String(value));
  }
  const text = found.toString();
  return text ? `?${text}` : "";
}

async function detailOf(response: Response): Promise<unknown> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    return body.detail ?? body;
  } catch {
    return undefined;
  }
}

/** One server-sent event: its ``id``, ``event`` and ``data`` fields. */
export interface ServerSentEvent {
  id?: string;
  event?: string;
  data: string;
}

/** The events of a ``text/event-stream`` body, as they arrive. */
export async function* serverSentEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<ServerSentEvent> {
  const decoder = new TextDecoder();
  const reader = body.getReader();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, "\n");
      let end = buffer.indexOf("\n\n");
      while (end !== -1) {
        const parsed = parse(buffer.slice(0, end));
        buffer = buffer.slice(end + 2);
        if (parsed) yield parsed;
        end = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parse(block: string): ServerSentEvent | undefined {
  const event: ServerSentEvent = { data: "" };
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "data") data.push(value);
    else if (field === "id") event.id = value;
    else if (field === "event") event.event = value;
  }
  if (data.length === 0) return undefined;
  event.data = data.join("\n");
  return event;
}

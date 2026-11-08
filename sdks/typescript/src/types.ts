/** What the AIOS API sends and takes. Fields AIOS may add later are allowed for: read what you
 * need, and ignore the rest. */

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export interface Me {
  principal: string;
  tenant: string;
  roles: string[];
}

export interface AppSummary {
  app: string;
  version: string;
  agents: string[];
  workflows: string[];
}

export interface AppDescription {
  app: string;
  version: string;
  description: string;
  entrypoints: { type: string; workflow: string }[];
  agents: { agent: string; version: string; runtime: string; capabilities: string[]; tools: string[] }[];
  humans: { capability: string; assignees: string[] }[];
  workflows: {
    workflow: string;
    version: string;
    steps: { id: string; type: string; capability: string | null; tool: string | null; needs: string[] }[];
  }[];
  policies: string[];
  permissions: { tools: string[] };
}

export interface RunRequest {
  /** What a chat entry point is asked. */
  message?: string;
  /** Or the workflow's input itself. */
  input?: JsonValue;
  /** The strictest class of data in the request. */
  data_class?: "public" | "internal" | "confidential" | "restricted";
  /** Runs of one conversation share their session memory. */
  session?: string;
}

export interface RunStarted {
  run_id: string;
  app: string;
  events: string;
}

export type RunStatus = "running" | "paused" | "completed" | "failed" | "cancelled";

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

export interface Run {
  run_id: string;
  app: string;
  status: RunStatus;
  waiting: string[];
  tainted: boolean;
  taint: string[];
  steps: Record<string, string>;
  output: JsonValue;
  usage: Usage | null;
  error: { code: string; message: string; retryable?: boolean } | null;
  events: number;
  source: Record<string, JsonValue> | null;
}

export interface RunSummary {
  run_id: string;
  status: string;
  started_at: string | null;
  [key: string]: JsonValue;
}

/** One event of a run, in the AIOS envelope (CloudEvents 1.0 and AIOS attributes). */
export interface RunEvent {
  id: string;
  type: string;
  source: string;
  time: string;
  tenantid: string;
  appid: string;
  runid?: string;
  principal: string;
  data?: JsonValue;
  [key: string]: JsonValue | undefined;
}

export type ApprovalStatus = "pending" | "granted" | "denied" | "expired" | "cancelled";

export interface Approval {
  approval: string;
  app: string;
  run: string | null;
  status: ApprovalStatus;
  summary: string;
  action: string | null;
  /** Exactly what would be done. */
  payload: JsonValue;
  risk: "low" | "medium" | "high" | "critical";
  policies: string[];
  /** The agent's own words: a claim, not a fact. */
  rationale: string | null;
  tainted: boolean;
  initiator: string | null;
  approvers: string[];
  quorum: number;
  votes: { by: string; comment: string | null }[];
  /** What the tool says it would do: a dry run. */
  preview: JsonValue;
  edits: Record<string, JsonValue> | null;
  expires_at: string;
  by: string | null;
  comment: string | null;
  decided_at: string | null;
}

export interface HumanTask {
  task: string;
  capability: string;
  assignees: string[];
  description: string;
  input: JsonValue;
  output_schema: Record<string, JsonValue> | null;
  step: string | null;
  app: string;
  run: string | null;
  status: "pending" | "completed" | "cancelled";
  by: string | null;
  output: JsonValue;
  comment: string | null;
}

export interface FeedbackRequest {
  /** About one step; the whole run if not given. */
  step?: string;
  rating?: "good" | "bad";
  /** What a right result would have been. */
  correction?: JsonValue;
  comment?: string;
  /** Keep it in the App's memory as well, for its agents. */
  remember?: boolean;
}

export interface Feedback extends FeedbackRequest {
  feedback: string;
  run: string;
  at: string;
  by: string;
  memory: string | null;
}

export interface SchemaRegistry {
  events: {
    type: string;
    version: number;
    kind: string;
    emitted_by: string;
    description: string;
    schema: string | null;
  }[];
  manifests: { kind: string; version: string; schema: string }[];
  envelope: string;
}

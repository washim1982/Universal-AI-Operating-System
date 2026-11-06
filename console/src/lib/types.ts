/** What the AIOS API sends. `tests/unit/api/test_approvals_api.py` keeps it to these fields. */

export interface Me {
  principal: string;
  tenant: string;
  roles: string[];
}

export interface App {
  app: string;
  version: string;
}

export type RunStatus = "running" | "paused" | "completed" | "failed" | "cancelled";

/** A replay answers every call from a run's record; a fork starts again from a step with side
 * effects held back; a retry starts again from a step for real (FR-OBS-04, FR-WF-11). */
export type RestartMode = "replay" | "fork" | "retry";

/** The run a replay, fork or retry comes from, and what it changed. */
export interface RunSource {
  run: string;
  mode: RestartMode;
  step: string | null;
  model: string | null;
  live: boolean;
  /** Steps whose results it kept from the source rather than ran. */
  copied: string[];
}

export interface Run {
  run_id: string;
  app: string;
  status: RunStatus;
  workflow: string | null;
  started_at: string | null;
  updated_at: string;
  on_behalf_of: string | null;
  tainted: boolean;
  cost_usd: number;
  source_run?: string | null;
  mode?: RestartMode | null;
}

export interface RunDetails {
  run_id: string;
  app: string;
  status: RunStatus;
  waiting: string[];
  tainted: boolean;
  taint: string[];
  steps: Record<string, string>;
  output: unknown;
  usage: { cost_usd?: number } | null;
  error: { code?: string; message?: string } | null;
  events: number;
  source?: RunSource | null;
}

export interface RunEvent {
  id: string;
  type: string;
  time: string;
}

export type ApprovalStatus = "pending" | "granted" | "denied" | "expired" | "cancelled";

export interface Approval {
  approval: string;
  app: string;
  run: string | null;
  status: ApprovalStatus;
  summary: string;
  action: string | null;
  payload: unknown;
  risk: "low" | "medium" | "high" | "critical";
  policies: string[];
  rationale: string | null;
  tainted: boolean;
  taint: string[];
  requested_by: string;
  initiator: string | null;
  requested_at: string;
  expires_at: string;
  data_class: string;
  by: string | null;
  comment: string | null;
  decided_at: string | null;
}

// --------------------------------------------------------------------------- the trace (FR-OBS-03)

export type StepStatus = "pending" | "running" | "waiting" | "completed" | "failed";

export interface ModelCall {
  provider: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  cost_usd: number;
  latency_ms: number;
  at: string;
  /** Answered from the source run's record: no model was asked. */
  replayed?: boolean;
}

export interface ToolCall {
  tool: string;
  status: "requested" | "completed" | "failed";
  latency_ms: number | null;
  arguments: unknown;
  error: string | null;
  at: string;
  /** A side effect held back: the tool wasn't called. */
  stubbed?: boolean;
  /** Answered from the source run's record. */
  replayed?: boolean;
}

export interface Decision {
  effect: "allowed" | "denied" | "escalated";
  action: string;
  resource: string;
  reason: string;
  policies: string[];
  at: string;
}

export interface MemoryRead {
  operation: string;
  scopes: string[];
  items: number;
  at: string;
}

export interface ApprovalTrace {
  approval: string;
  status: "requested" | "granted" | "denied" | "expired" | "cancelled";
  summary: string;
  risk: string | null;
  by: string | null;
  comment: string | null;
  at: string;
}

export interface TraceNode {
  id: string;
  type: string;
  needs: string[];
  status: StepStatus;
  attempts: number;
  capability: string | null;
  tool: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  input: unknown;
  output: unknown;
  error: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  tainted: boolean;
  /** Kept from the source run, not run in this one. */
  copied?: boolean;
  model_calls: ModelCall[];
  tool_calls: ToolCall[];
  decisions: Decision[];
  memory: MemoryRead[];
  approvals: ApprovalTrace[];
}

export interface TraceEdge {
  source: string;
  target: string;
  loop: boolean;
}

export interface Moment {
  id: string;
  type: string;
  at: string;
  offset_ms: number;
  step: string | null;
}

export interface Trace {
  run_id: string;
  app: string;
  workflow: string | null;
  version: string | null;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  tainted: boolean;
  source?: RunSource | null;
  nodes: TraceNode[];
  edges: TraceEdge[];
  timeline: Moment[];
}

/** One run's side of a step, in a comparison. */
export interface Side {
  status: string;
  output: unknown;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number | null;
  copied: boolean;
  stubbed: number;
  error: string | null;
}

export interface StepComparison {
  step: string;
  a: Side;
  b: Side;
  same_output: boolean;
  same_status: boolean;
}

/** Two runs side by side, step by step: usually a run and its replay or fork. */
export interface Comparison {
  a: string;
  b: string;
  status: [string, string];
  output: [unknown, unknown];
  same_output: boolean;
  cost_usd: [number, number];
  steps: StepComparison[];
}

// --------------------------------------------------------------------------- apps (FR-UI-01)

export interface Grants {
  tools: string[];
  data_classes: string[];
  memory_scopes: string[];
  hosts: string[];
}

export interface Requested extends Grants {
  per_run_usd: number | null;
  monthly_usd: number | null;
  starts: string[];
}

export type InstallStatus = "awaiting-consent" | "active" | "uninstalled";

export interface Installation {
  tenant: string;
  app: string;
  status: InstallStatus;
  version: string | null;
  pending: string | null;
  config: Record<string, unknown>;
  pending_config: Record<string, unknown> | null;
  grants: Grants;
  refused: Grants;
  granted_by: string | null;
  updated_at: string;
  requested?: Requested;
}

export interface Version {
  version: string;
  digest: string;
  running: boolean;
  pending: boolean;
  requested: Requested;
}

// --------------------------------------------------------------------------- agents (FR-UI-02)

export interface Agent {
  app: string;
  agent: string;
  version: string;
  capabilities: string[];
  tools: string[];
  refused_tools: string[];
  memory_read: string[];
  memory_write: string[];
  suspended: boolean;
  suspension: string | null;
  completed: number;
  failed: number;
  last_active: string | null;
}

export interface Suspension {
  kind: "agent" | "app" | "tenant";
  name: string;
  reason: string;
  by: string;
  at: string;
}

// --------------------------------------------------------------------------- costs (FR-UI-05)

export interface Spend {
  key: string;
  cost_usd: number;
  calls: number;
  input_tokens: number;
  output_tokens: number;
}

export interface BudgetAlert {
  type: "budget.warning" | "budget.exceeded";
  app: string;
  budget: string;
  level: string | null;
  limit: number | null;
  observed: number | null;
  run: string | null;
  at: string;
}

export interface Budget {
  level: "tenant" | "app" | "user";
  subject: string;
  limit_usd: number;
  soft_usd: number;
  spent_usd: number;
}

export interface CostReport {
  month: string;
  cost_usd: number;
  calls: number;
  by_app: Spend[];
  by_agent: Spend[];
  by_model: Spend[];
  by_user: Spend[];
  alerts: BudgetAlert[];
  budgets: Budget[];
}

// --------------------------------------------------------------------------- policies (FR-UI-06)

export interface PolicyRule {
  id: string;
  kind: string;
  approvers: string[];
}

export interface PolicyView {
  app: string;
  policies: Array<{
    metadata: { name: string; version: string };
    spec: Record<string, unknown>;
  }>;
  rules: PolicyRule[];
  cedar: string;
}

export interface Verdict {
  effect: "allow" | "deny" | "require_approval";
  policies: string[];
  approvers: string[];
  reason: string;
}

export type SimulatedAction =
  | "tool.invoke"
  | "memory.read"
  | "memory.write"
  | "task.dispatch"
  | "model.route";

// --------------------------------------------------------------------------- memory (FR-UI-07)

export interface MemoryItem {
  id: string;
  tenant: string;
  scope: string;
  owner: string;
  content: string;
  subject: string | null;
  kind: string;
  retention: string;
  expires_at: string | null;
  sensitivity: string;
  importance: number;
  metadata: Record<string, unknown>;
  provenance: { source_uri?: string | null; [key: string]: unknown };
  document: string | null;
  version: number;
  updated_at: string;
  score?: number;
}

// --------------------------------------------------------------------------- events (FR-UI-09)

export interface StoredEvent {
  position: number;
  id: string;
  type: string;
  time: string;
  source: string;
  appid: string;
  tenantid: string;
  runid?: string;
  principal: string;
  onbehalfof?: string;
  dataclass: string;
  tainted?: boolean;
  data?: unknown;
}

export interface DeadLetter {
  id: string;
  group: string;
  event: StoredEvent;
  attempts: number;
  reason: string;
  dead_at: string;
}

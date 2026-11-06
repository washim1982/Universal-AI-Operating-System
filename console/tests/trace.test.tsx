// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StepDetails } from "@/components/StepDetails";
import { firstChoice, TraceExplorer } from "@/components/TraceExplorer";
import type { Trace, TraceNode } from "@/lib/types";

// React Flow draws on a canvas jsdom doesn't have. The graph is a picture of what the list says.
vi.mock("@xyflow/react", () => ({
  ReactFlow: ({ nodes, children }: { nodes: Array<{ id: string }>; children: ReactNode }) => (
    <div data-testid="graph">
      {nodes.map((n) => (
        <span key={n.id}>node {n.id}</span>
      ))}
      {children}
    </div>
  ),
  Background: () => null,
  Handle: () => null,
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" },
  useReactFlow: () => ({ fitView: async () => true }),
  useStore: () => 0,
}));

function step(id: string, fields: Partial<TraceNode> = {}): TraceNode {
  return {
    id,
    type: "task",
    needs: [],
    status: "completed",
    attempts: 1,
    capability: `${id}.work`,
    tool: null,
    started_at: "2026-11-02T10:00:00+00:00",
    ended_at: "2026-11-02T10:00:02+00:00",
    duration_ms: 2000,
    input: { question: "why?" },
    output: { answer: "because" },
    error: null,
    input_tokens: 120,
    output_tokens: 40,
    cost_usd: 0.0021,
    tainted: false,
    model_calls: [],
    tool_calls: [],
    decisions: [],
    memory: [],
    approvals: [],
    ...fields,
  };
}

const TRACE: Trace = {
  run_id: "01RUN",
  app: "desk",
  workflow: "ticket",
  version: "1.2.0",
  status: "failed",
  started_at: "2026-11-02T10:00:00+00:00",
  ended_at: "2026-11-02T10:00:05+00:00",
  duration_ms: 5000,
  input_tokens: 240,
  output_tokens: 80,
  cost_usd: 0.0042,
  tainted: true,
  nodes: [
    step("triage"),
    step("refund", {
      status: "failed",
      error: "policy.denied: refused",
      tainted: true,
      tool_calls: [
        {
          tool: "stripe.refund.create",
          status: "failed",
          latency_ms: 3,
          arguments: { amountUSD: 500 },
          error: "denied by policy",
          at: "2026-11-02T10:00:03+00:00",
        },
      ],
      decisions: [
        {
          effect: "denied",
          action: "tool.invoke",
          resource: "stripe.refund.create",
          reason: "denied by policy",
          policies: ["consent/deny/0"],
          at: "2026-11-02T10:00:03+00:00",
        },
      ],
    }),
  ],
  edges: [{ source: "triage", target: "refund", loop: false }],
  timeline: [
    { id: "e1", type: "run.started", at: "2026-11-02T10:00:00+00:00", offset_ms: 0, step: null },
    { id: "e2", type: "step.started", at: "2026-11-02T10:00:01+00:00", offset_ms: 1000, step: "triage" },
    { id: "e3", type: "step.failed", at: "2026-11-02T10:00:04+00:00", offset_ms: 4000, step: "refund" },
  ],
};

afterEach(cleanup);

describe("the Trace Explorer", () => {
  it("opens on the step that needs attention", () => {
    expect(firstChoice(TRACE)).toBe("refund");
    expect(firstChoice({ ...TRACE, nodes: [step("a"), step("b")] })).toBe("a");
    expect(firstChoice({ ...TRACE, nodes: [] })).toBeNull();
  });

  it("draws every step, and lists them for a keyboard", () => {
    render(<TraceExplorer trace={TRACE} />);
    expect(screen.getByText("node triage")).toBeTruthy();
    const steps = within(screen.getByRole("navigation", { name: "Steps" }));
    expect(steps.getAllByRole("button")).toHaveLength(2);
  });

  it("marks the steps a fork kept from its source", () => {
    render(<TraceExplorer trace={{ ...TRACE, nodes: [step("triage", { copied: true }), step("refund")] }} />);
    const steps = within(screen.getByRole("navigation", { name: "Steps" }));
    expect(steps.getByRole("button", { name: /triage/ }).textContent).toContain("copied");
    expect(steps.getByRole("button", { name: /refund/ }).textContent).not.toContain("copied");
  });

  it("shows the chosen step, and another when it is chosen", () => {
    render(<TraceExplorer trace={TRACE} />);
    expect(screen.getByRole("article", { name: "Step refund" })).toBeTruthy();
    const steps = within(screen.getByRole("navigation", { name: "Steps" }));
    fireEvent.click(steps.getByRole("button", { name: /triage/ }));
    expect(screen.getByRole("article", { name: "Step triage" })).toBeTruthy();
    expect(steps.getByRole("button", { name: /triage/ }).getAttribute("aria-current")).toBe("true");
  });

  it("a moment on the timeline leads to its step", () => {
    render(<TraceExplorer trace={TRACE} />);
    fireEvent.click(screen.getByRole("button", { name: "triage" }));
    expect(screen.getByRole("article", { name: "Step triage" })).toBeTruthy();
  });

  it("says what the run cost and took", () => {
    render(<TraceExplorer trace={TRACE} />);
    expect(screen.getByText("$0.0042")).toBeTruthy();
    expect(screen.getByText("5.0 s")).toBeTruthy();
    expect(screen.getByText(/240 in · 80 out tokens/)).toBeTruthy();
  });

  it("a run with no steps yet says so", () => {
    render(<TraceExplorer trace={{ ...TRACE, nodes: [], edges: [] }} />);
    expect(screen.getByText("This run has no steps yet.")).toBeTruthy();
    expect(screen.queryByTestId("graph")).toBeNull();
  });
});

describe("a step's details", () => {
  it("shows why it failed, and the decision that stopped it", () => {
    render(<StepDetails step={TRACE.nodes[1] as TraceNode} />);
    expect(screen.getByRole("alert").textContent).toBe("policy.denied: refused");
    expect(screen.getByText("denied")).toBeTruthy();
    expect(screen.getByText("(consent/deny/0)")).toBeTruthy();
    expect(screen.getByText("This step worked with untrusted content.")).toBeTruthy();
  });

  it("lists model calls with their tokens, cost and time", () => {
    const call = {
      provider: "anthropic",
      model: "claude-opus-5-5",
      input_tokens: 1200,
      output_tokens: 300,
      cached_tokens: 1000,
      cost_usd: 0.0123,
      latency_ms: 850,
      at: "2026-11-02T10:00:01+00:00",
    };
    render(<StepDetails step={step("draft", { model_calls: [call] })} />);
    expect(screen.getByText("anthropic/claude-opus-5-5")).toBeTruthy();
    expect(screen.getByText("1,200")).toBeTruthy();
    expect(screen.getByText("1,000")).toBeTruthy();
    expect(screen.getByText("850 ms")).toBeTruthy();
  });

  it("shows an approval and who answered it", () => {
    const approval = {
      approval: "01A",
      status: "granted" as const,
      summary: "Refund $120",
      risk: "high",
      by: "user:lee@acme",
      comment: "checked the order",
      at: "2026-11-02T10:00:02+00:00",
    };
    render(<StepDetails step={step("refund", { approvals: [approval] })} />);
    expect(screen.getByText("granted")).toBeTruthy();
    expect(screen.getByText(/by lee/)).toBeTruthy();
    expect(screen.getByText("checked the order")).toBeTruthy();
  });

  it("keeps restricted data out", () => {
    render(<StepDetails step={step("pay", { input: { redacted: "restricted" }, output: null })} />);
    expect(screen.getByText("Restricted data isn't shown in a trace.")).toBeTruthy();
  });

  it("shows hostile text as text", () => {
    const hostile = '<img src=x onerror="alert(1)">';
    const { container } = render(<StepDetails step={step("read", { output: { page: hostile } })} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("onerror");
  });
});

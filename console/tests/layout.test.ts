import { describe, expect, it } from "vitest";

import { ACROSS, depth, DOWN, levels, place } from "@/lib/layout";
import type { TraceEdge, TraceNode } from "@/lib/types";

function node(id: string): TraceNode {
  return {
    id,
    type: "task",
    needs: [],
    status: "pending",
    attempts: 0,
    capability: null,
    tool: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    input: null,
    output: null,
    error: null,
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: 0,
    tainted: false,
    model_calls: [],
    tool_calls: [],
    decisions: [],
    memory: [],
    approvals: [],
  };
}

const edge = (source: string, target: string, loop = false): TraceEdge => ({ source, target, loop });

describe("where steps go", () => {
  it("puts a step below everything it needs", () => {
    const nodes = ["plan", "a", "b", "join"].map(node);
    const edges = [edge("plan", "a"), edge("plan", "b"), edge("a", "join"), edge("b", "join")];
    expect(Object.fromEntries(levels(nodes, edges))).toEqual({ plan: 0, a: 1, b: 1, join: 2 });
  });

  it("goes by the furthest need, not the nearest", () => {
    const nodes = ["a", "b", "c"].map(node);
    const edges = [edge("a", "b"), edge("b", "c"), edge("a", "c")];
    expect(levels(nodes, edges).get("c")).toBe(2);
  });

  it("ignores a loop back, or it would never settle", () => {
    const nodes = ["draft", "review"].map(node);
    const edges = [edge("draft", "review"), edge("review", "draft", true)];
    expect(Object.fromEntries(levels(nodes, edges))).toEqual({ draft: 0, review: 1 });
  });

  it("stops at a cycle instead of hanging", () => {
    const nodes = ["a", "b"].map(node);
    expect(() => levels(nodes, [edge("a", "b"), edge("b", "a")])).not.toThrow();
  });

  it("ignores edges to steps it doesn't have", () => {
    expect(levels([node("a")], [edge("ghost", "a")]).get("a")).toBe(0);
  });

  it("sets steps of one level side by side, and centres a narrow one", () => {
    const nodes = ["plan", "a", "b", "end"].map(node);
    const edges = [edge("plan", "a"), edge("plan", "b"), edge("a", "end"), edge("b", "end")];
    const placed = Object.fromEntries(place(nodes, edges).map((p) => [p.id, p]));
    expect(placed.a).toEqual({ id: "a", x: 0, y: DOWN });
    expect(placed.b).toEqual({ id: "b", x: ACROSS, y: DOWN });
    expect(placed.plan?.x).toBe(ACROSS / 2);
    expect(placed.end?.y).toBe(2 * DOWN);
  });

  it("knows how deep the graph is", () => {
    const nodes = ["a", "b", "c"].map(node);
    expect(depth(place(nodes, [edge("a", "b"), edge("b", "c")]))).toBe(3);
    expect(depth([])).toBe(0);
  });
});

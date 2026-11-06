/** Where each step goes in the Trace Explorer: top to bottom in the order steps can run.
 *
 * A step's level is one more than the furthest of the steps it needs, so everything a step waits
 * for is above it. Most workflows are chains, and a page is taller than it is wide, so they read
 * downwards. Loop edges point back up and don't count, or a loop would never settle. */

import type { TraceEdge, TraceNode } from "./types";

/** Distance between steps side by side, and between levels. */
export const ACROSS = 220;
export const DOWN = 120;

export interface Placed {
  id: string;
  x: number;
  y: number;
}

export function levels(nodes: TraceNode[], edges: TraceEdge[]): Map<string, number> {
  const known = new Set(nodes.map((node) => node.id));
  const needs = new Map<string, string[]>(nodes.map((node) => [node.id, []]));
  for (const edge of edges) {
    if (!edge.loop && known.has(edge.source) && known.has(edge.target)) {
      needs.get(edge.target)?.push(edge.source);
    }
  }
  const found = new Map<string, number>();
  const visiting = new Set<string>();
  function level(id: string): number {
    const done = found.get(id);
    if (done !== undefined) return done;
    if (visiting.has(id)) return 0; // a cycle the workflow shouldn't have: stop, don't hang
    visiting.add(id);
    const before = needs.get(id) ?? [];
    const value = before.length === 0 ? 0 : 1 + Math.max(...before.map(level));
    visiting.delete(id);
    found.set(id, value);
    return value;
  }
  for (const node of nodes) level(node.id);
  return found;
}

export function place(nodes: TraceNode[], edges: TraceEdge[]): Placed[] {
  const found = levels(nodes, edges);
  const widths = new Map<number, number>();
  for (const value of found.values()) widths.set(value, (widths.get(value) ?? 0) + 1);
  const widest = Math.max(0, ...widths.values());
  const used = new Map<number, number>();
  return nodes.map((node) => {
    const level = found.get(node.id) ?? 0;
    const slot = used.get(level) ?? 0;
    used.set(level, slot + 1);
    const offset = ((widest - (widths.get(level) ?? 0)) * ACROSS) / 2; // centre a narrow level
    return { id: node.id, x: offset + slot * ACROSS, y: level * DOWN };
  });
}

/** How many levels deep the graph is, for how tall to draw it. */
export function depth(placed: Placed[]): number {
  return placed.length === 0 ? 0 : Math.max(...placed.map((p) => p.y)) / DOWN + 1;
}

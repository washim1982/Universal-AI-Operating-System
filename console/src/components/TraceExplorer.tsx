"use client";

/** A run as a graph of its steps, and what the chosen step did (FR-OBS-03, FR-UI-03).
 *
 * The graph is for seeing the shape. Everything in it is also in the step list and the details
 * beside it, which a keyboard and a screen reader can reach. */

import {
  Background,
  type Edge,
  Handle,
  MarkerType,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
  useReactFlow,
  useStore,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useState } from "react";

import { count, duration, usd } from "@/lib/format";
import { depth, DOWN, place } from "@/lib/layout";
import type { Trace, TraceNode } from "@/lib/types";

import { StepDetails } from "./StepDetails";

type StepData = { step: TraceNode; chosen: boolean };
type StepNode = Node<StepData, "step">;

function Step({ data }: NodeProps<StepNode>) {
  const { step, chosen } = data;
  return (
    <div className={`step ${step.status}${chosen ? " chosen" : ""}`}>
      <Handle type="target" position={Position.Top} id="in" />
      <Handle type="target" position={Position.Right} id="back-in" className="back" />
      <strong>{step.id}</strong>
      <span className="step-what">{step.capability ?? step.tool ?? step.type}</span>
      <span className="step-figures">
        {step.status}
        {step.duration_ms !== null && ` · ${duration(step.duration_ms)}`}
        {step.cost_usd > 0 && ` · ${usd(step.cost_usd)}`}
      </span>
      {step.tainted && <span className="badge warning">untrusted</span>}
      {step.copied && <span className="badge muted">copied</span>}
      <Handle type="source" position={Position.Bottom} id="out" />
      <Handle type="source" position={Position.Right} id="back-out" className="back" />
    </div>
  );
}

const NODE_TYPES = { step: Step };
const FIT = { maxZoom: 1, padding: 0.15 };

/** Fits the steps again when the graph changes width: React Flow fits once, when it starts, and a
 * window turned or narrowed would otherwise leave them out of sight. */
function Refit() {
  const { fitView } = useReactFlow();
  const width = useStore((state) => state.width);
  useEffect(() => {
    if (width > 0) void fitView(FIT);
  }, [width, fitView]);
  return null;
}

/** The step to open first: whatever needs attention, or else the first. */
export function firstChoice(trace: Trace): string | null {
  const wanted = trace.nodes.find((n) => n.status === "failed" || n.status === "waiting");
  return (wanted ?? trace.nodes[0])?.id ?? null;
}

export function TraceExplorer({ trace }: { trace: Trace }) {
  const [chosen, choose] = useState<string | null>(() => firstChoice(trace));
  const positions = useMemo(() => place(trace.nodes, trace.edges), [trace.nodes, trace.edges]);
  const nodes: StepNode[] = trace.nodes.map((step, index) => ({
    id: step.id,
    type: "step",
    position: { x: positions[index]?.x ?? 0, y: positions[index]?.y ?? 0 },
    data: { step, chosen: step.id === chosen },
  }));
  // A loop goes out and back up on the right, so that it never crosses the steps between.
  const edges: Edge[] = trace.edges.map((edge) => ({
    id: `${edge.source}->${edge.target}${edge.loop ? ":loop" : ""}`,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.loop ? "back-out" : "out",
    targetHandle: edge.loop ? "back-in" : "in",
    type: edge.loop ? "smoothstep" : "default",
    pathOptions: edge.loop ? { offset: 40 } : undefined,
    className: edge.loop ? "loop" : undefined,
    label: edge.loop ? "may loop back" : undefined,
    markerEnd: { type: MarkerType.ArrowClosed },
  }));
  const height = Math.min(640, Math.max(200, depth(positions) * DOWN + 60));
  const step = trace.nodes.find((node) => node.id === chosen) ?? null;
  return (
    <section className="stack" aria-label="Trace">
      <h2>Trace</h2>
      <p className="card-meta">
        <span>
          {trace.workflow} {trace.version && <small>v{trace.version}</small>}
        </span>
        {trace.duration_ms !== null && <span>{duration(trace.duration_ms)}</span>}
        <span>
          {count(trace.input_tokens)} in · {count(trace.output_tokens)} out tokens
        </span>
        <span>{usd(trace.cost_usd)}</span>
      </p>
      {trace.nodes.length > 0 && (
        <div
          className="graph"
          role="img"
          aria-label={`The steps of ${trace.workflow ?? "the run"}`}
          style={{ height }}
        >
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            onNodeClick={(_, node) => choose(node.id)}
            nodesDraggable={false}
            nodesConnectable={false}
            disableKeyboardA11y
            deleteKeyCode={null}
            fitView
            fitViewOptions={FIT}
            minZoom={0.3}
          >
            <Background />
            <Refit />
          </ReactFlow>
        </div>
      )}
      <div className="trace">
        <nav aria-label="Steps">
          <ul className="plain steps">
            {trace.nodes.map((node) => (
              <li key={node.id}>
                <button
                  type="button"
                  className={`link step-link ${node.status}`}
                  aria-current={node.id === chosen ? "true" : undefined}
                  onClick={() => choose(node.id)}
                >
                  {node.id} <small>{node.status}</small>
                  {node.copied && <small> · copied</small>}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        {step ? <StepDetails step={step} /> : <p className="empty">This run has no steps yet.</p>}
      </div>
      <section>
        <h3>Timeline</h3>
        <ol className="plain timeline">
          {trace.timeline.map((moment) => (
            <li key={moment.id}>
              <time dateTime={moment.at}>+{duration(moment.offset_ms)}</time>{" "}
              <code>{moment.type}</code>
              {moment.step && (
                <>
                  {" "}
                  <button type="button" className="link" onClick={() => choose(moment.step)}>
                    {moment.step}
                  </button>
                </>
              )}
            </li>
          ))}
        </ol>
      </section>
    </section>
  );
}

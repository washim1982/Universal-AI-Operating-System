"use client";

import { startTransition, useActionState } from "react";

import type { SimulationState } from "@/app/(console)/policies/actions";

type Ask = (state: SimulationState, form: FormData) => Promise<SimulationState>;

const SAYS = { allow: "Allowed", deny: "Refused", require_approval: "Needs a person" } as const;
const TONE = { allow: "good", deny: "danger", require_approval: "warning" } as const;

/** "Would this be allowed?" (FR-UI-06), asked of the same engine that decides for real. */
export function Simulator({ ask, agents }: { ask: Ask; agents: string[] }) {
  const [state, submit, pending] = useActionState<SimulationState, FormData>(ask, {});
  return (
    // Sent by hand, not as the form's action, so that the question stays on the form to change.
    <form
      className="stack simulator"
      aria-label="Policy simulator"
      aria-busy={pending}
      onSubmit={(event) => {
        event.preventDefault();
        const asked = new FormData(event.currentTarget);
        startTransition(() => submit(asked));
      }}
    >
      <div className="row">
        <label>
          Agent
          <select name="agent" required defaultValue={agents[0]}>
            {agents.map((agent) => (
              <option key={agent}>{agent}</option>
            ))}
          </select>
        </label>
        <label>
          Would do
          <select name="would" defaultValue="tool.invoke">
            <option value="tool.invoke">call a tool</option>
            <option value="memory.read">read memory</option>
            <option value="memory.write">write memory</option>
            <option value="task.dispatch">take a task</option>
            <option value="model.route">call a model</option>
          </select>
        </label>
        <label>
          Tool, or memory scope
          <input name="resource" placeholder="stripe.refund.create" />
        </label>
      </div>
      <div className="row">
        <label>
          For (optional)
          <input name="on_behalf_of" placeholder="user:lee@acme" />
        </label>
        <label>
          Side effect, for a tool AIOS doesn&apos;t know
          <select name="side_effect" defaultValue="">
            <option value="">as registered</option>
            <option>read</option>
            <option>write</option>
            <option value="external_comm">external comm</option>
            <option>financial</option>
            <option>destructive</option>
            <option value="code_exec">code exec</option>
          </select>
        </label>
      </div>
      <label>
        Tool input, as JSON (optional)
        <textarea name="input" rows={2} placeholder='{"amountUSD": 120}' spellCheck={false} />
      </label>
      <div className="row">
        <label className="check">
          <input type="checkbox" name="tainted" value="1" /> after reading untrusted content
        </label>
        <label className="check">
          <input type="checkbox" name="approved" value="1" /> already approved by a person
        </label>
      </div>
      <div className="row">
        <button className="button primary" type="submit" disabled={pending}>
          Ask
        </button>
      </div>
      {state.error && (
        <p className="notice danger" role="alert">
          {state.error}
        </p>
      )}
      {state.verdict && (
        <div className="notice" role="status">
          <p>
            <span className={`badge ${TONE[state.verdict.effect]}`}>{SAYS[state.verdict.effect]}</span>{" "}
            {state.verdict.reason}
          </p>
          {state.verdict.approvers.length > 0 && <p>Who may approve: {state.verdict.approvers.join(", ")}</p>}
          {state.verdict.policies.length > 0 && (
            <p className="hint">Decided by {state.verdict.policies.join(", ")}</p>
          )}
        </div>
      )}
    </form>
  );
}

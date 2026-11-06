import { restart } from "@/app/(console)/runs/actions";
import type { RunStatus } from "@/lib/types";

import { ActionForm } from "./ActionForm";

const FINISHED: readonly RunStatus[] = ["completed", "failed", "cancelled"];

/** Run a finished run again (FR-OBS-04, FR-WF-11): from its record, or from a step with changes.
 * AIOS decides whether this person may; a retry needs more than starting a run. */
export function RestartForm({
  app,
  id,
  status,
  steps,
  failed,
}: {
  app: string;
  id: string;
  status: RunStatus;
  /** The workflow's steps, in order. */
  steps: string[];
  /** The step that failed, where a retry starts by default. */
  failed: string | null;
}) {
  if (!FINISHED.includes(status)) return null;
  const again = restart.bind(null, app, id);
  return (
    <section className="stack" aria-label="Run again">
      <h2>Run again</h2>
      <ActionForm action={again} label="Replay">
        <button className="button" type="submit" name="mode" value="replay">
          Replay
        </button>
        <p className="hint">
          Every model and tool call is answered from this run&apos;s record: nothing live, nothing
          spent. A replay that would have to do something new stops and says where.
        </p>
      </ActionForm>
      <ActionForm action={again} className="stack" label="Fork or retry" keep>
        <label>
          From step
          <select name="step" defaultValue={failed ?? steps[0] ?? ""}>
            {failed && <option value="">Where it failed ({failed})</option>}
            {steps.map((step) => (
              <option key={step} value={step}>
                {step}
              </option>
            ))}
          </select>
        </label>
        <label>
          New run input <span className="hint">JSON or text. Empty keeps the original.</span>
          <textarea name="input" rows={3} spellCheck={false} />
        </label>
        <label>
          New input for that step{" "}
          <span className="hint">
            Replaces the step&apos;s whole input, so give all of it: its Input is under the trace below.
            JSON, or text for a step that takes text. Empty keeps the original.
          </span>
          <textarea name="step_input" rows={3} spellCheck={false} />
        </label>
        <label>
          Model <span className="hint">A model the App may use, for every call. Empty keeps the original.</span>
          <input name="model" placeholder="provider/model" spellCheck={false} />
        </label>
        <fieldset>
          <legend>Fork</legend>
          <p className="hint">
            Steps before it keep their results. Side effects are held back: a tool that changes
            something isn&apos;t called, a message isn&apos;t sent, and nothing is remembered.
          </p>
          <button className="button" type="submit" name="mode" value="fork">
            Fork
          </button>
        </fieldset>
        <fieldset>
          <legend>Retry</legend>
          <label className="check">
            <input type="checkbox" name="really" />
            Do its side effects again, for real
          </label>
          <button className="button danger" type="submit" name="mode" value="retry">
            Retry for real
          </button>
        </fieldset>
      </ActionForm>
    </section>
  );
}

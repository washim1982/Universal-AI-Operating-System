import { control } from "@/app/(console)/runs/actions";
import type { RunStatus } from "@/lib/types";

import { ActionForm } from "./ActionForm";

/** The engine's name for a pause someone asked for. A run also waits for approvals and signals,
 * which show as paused too, but only a requested pause can be resumed. */
const HOLD = "request";

/** What can be done to a run, given where it is. Finished runs offer nothing. */
export function RunControls({
  app,
  id,
  status,
  waiting,
}: {
  app: string;
  id: string;
  status: RunStatus;
  waiting: string[];
}) {
  if (status !== "running" && status !== "paused") return null;
  return (
    <ActionForm action={control.bind(null, app, id)} label="Run controls">
      {!waiting.includes(HOLD) ? (
        <button className="button" type="submit" name="control" value="pause">
          Pause
        </button>
      ) : (
        <button className="button" type="submit" name="control" value="resume">
          Resume
        </button>
      )}
      <button className="button danger" type="submit" name="control" value="cancel">
        Cancel run
      </button>
    </ActionForm>
  );
}

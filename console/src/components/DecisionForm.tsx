"use client";

import { useActionState } from "react";

import type { DecisionState } from "@/app/(console)/approvals/actions";
import { editable } from "@/lib/forms";

type Decide = (state: DecisionState, form: FormData) => Promise<DecisionState>;

function multiline(value: unknown): boolean {
  return typeof value === "string" ? value.includes("\n") || value.length > 80 : false;
}

/** Approve or deny. With ``changeable``, an approver may change the action's parameters before
 * saying yes (FR-HUM-06): AIOS checks the changed action against policy again, and refuses one
 * that would need an approval this one doesn't give. */
export function DecisionForm({
  action,
  changeable,
}: {
  action: Decide;
  changeable?: Record<string, unknown> | null;
}) {
  const [state, submit, pending] = useActionState<DecisionState, FormData>(action, {});
  return (
    <form action={submit} className="stack decision">
      {changeable && Object.keys(changeable).length > 0 && (
        <details className="edits">
          <summary>Change something before approving</summary>
          <p className="hint">
            Only a yes can change it. Text stays text; anything else is read as JSON. The changed
            action is checked against policy again before it runs.
          </p>
          {Object.entries(changeable).map(([name, value]) => (
            <label key={name}>
              <code>{name}</code>
              {multiline(value) ? (
                // a one-line input would drop the line breaks, and so change what is approved
                <textarea name={`edit.${name}`} defaultValue={editable(value)} rows={3} />
              ) : (
                <input name={`edit.${name}`} defaultValue={editable(value)} />
              )}
            </label>
          ))}
        </details>
      )}
      <label>
        Comment <span className="hint">(optional, kept on the record)</span>
        <textarea name="comment" rows={2} maxLength={2000} />
      </label>
      {state.error && (
        <p className="notice danger" role="alert">
          {state.error}
        </p>
      )}
      <div className="row">
        <button className="button primary" type="submit" name="verdict" value="approve" disabled={pending}>
          Approve
        </button>
        <button className="button danger" type="submit" name="verdict" value="deny" disabled={pending}>
          Deny
        </button>
      </div>
    </form>
  );
}

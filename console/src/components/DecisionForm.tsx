"use client";

import { useActionState } from "react";

import type { DecisionState } from "@/app/(console)/approvals/actions";

type Decide = (state: DecisionState, form: FormData) => Promise<DecisionState>;

export function DecisionForm({ action }: { action: Decide }) {
  const [state, submit, pending] = useActionState<DecisionState, FormData>(action, {});
  return (
    <form action={submit} className="stack decision">
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

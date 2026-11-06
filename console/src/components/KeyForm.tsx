"use client";

import { useActionState } from "react";

import { signInWithKey, type SignInState } from "@/app/login/actions";

export function KeyForm() {
  const [state, action, pending] = useActionState<SignInState, FormData>(signInWithKey, {});
  return (
    <form action={action} className="stack">
      <label>
        API key
        <input name="key" type="password" autoComplete="off" required />
      </label>
      {state.error && (
        <p className="notice danger" role="alert">
          {state.error}
        </p>
      )}
      <button className="button primary" type="submit" disabled={pending}>
        Sign in
      </button>
      <p className="hint">
        Create one with <code>aios platform key</code>.
      </p>
    </form>
  );
}

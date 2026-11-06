"use client";

import { startTransition, useActionState, type FormEvent, type ReactNode } from "react";

import type { FormState } from "@/lib/forms";

type Act = (state: FormState, form: FormData) => Promise<FormState>;

/** A form that sends to a server action and says how it went, without leaving the page. */
export function ActionForm({
  action,
  children,
  className = "row",
  label,
  keep = false,
}: {
  action: Act;
  children: ReactNode;
  className?: string;
  label?: string;
  /** Leave what was typed on the form after it is sent, to change and send again. React empties
   * a form whose action has run, which suits a button but not a form someone has filled in. */
  keep?: boolean;
}) {
  const [state, submit, pending] = useActionState<FormState, FormData>(action, {});
  const byHand = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const sent = new FormData(event.currentTarget, (event.nativeEvent as SubmitEvent).submitter);
    startTransition(() => submit(sent));
  };
  return (
    <form
      action={submit}
      onSubmit={keep ? byHand : undefined}
      className={className}
      aria-label={label}
      aria-busy={pending}
    >
      {children}
      {state.error && (
        <p className="notice danger" role="alert">
          {state.error}
        </p>
      )}
      {state.done && (
        <p className="notice" role="status">
          {state.done}
        </p>
      )}
    </form>
  );
}

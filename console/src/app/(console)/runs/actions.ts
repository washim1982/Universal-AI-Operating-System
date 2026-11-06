"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { ApiError, controlRun, restartRun, type Changes } from "@/lib/api";
import { changesFrom, type FormState } from "@/lib/forms";
import type { RestartMode } from "@/lib/types";

const WORDS = { pause: "Pausing", resume: "Resuming", cancel: "Cancelling" } as const;

/** Pause, resume or cancel a run. AIOS decides whether this person may. */
export async function control(app: string, id: string, _: FormState, form: FormData): Promise<FormState> {
  const action = form.get("control");
  if (action !== "pause" && action !== "resume" && action !== "cancel") {
    return { error: "Choose pause, resume or cancel." };
  }
  try {
    await controlRun(app, id, action);
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  revalidatePath(`/runs/${app}/${id}`);
  return { done: `${WORDS[action]} the run. It takes effect between steps.` };
}

/** Replay, fork or retry a finished run, then go to the new one. AIOS decides whether this
 * person may: a retry does things for real again, so it needs more than starting a run. */
export async function restart(
  app: string,
  id: string,
  _: FormState,
  form: FormData,
): Promise<FormState> {
  const mode = form.get("mode");
  if (mode !== "replay" && mode !== "fork" && mode !== "retry") {
    return { error: "Choose replay, fork or retry." };
  }
  const changes: Changes = mode === "replay" ? {} : changesFrom(form);
  if (mode === "fork" && !changes.step) return { error: "Choose the step to fork from." };
  if (mode === "retry" && form.get("really") !== "on") {
    return { error: "Tick the box first: a retry does its side effects again, for real." };
  }
  let started: { run_id: string };
  try {
    started = await restartRun(app, id, mode satisfies RestartMode, changes);
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  revalidatePath("/runs");
  redirect(`/runs/${encodeURIComponent(app)}/${encodeURIComponent(started.run_id)}`);
}

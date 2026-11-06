"use server";

import { revalidatePath } from "next/cache";

import { ApiError, discard, redrive } from "@/lib/api";
import type { FormState } from "@/lib/forms";

async function attempt(act: () => Promise<unknown>, done: string): Promise<FormState> {
  try {
    await act();
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  revalidatePath("/events");
  return { done };
}

/** Put a dead letter back on its queue, to be tried again from the start. */
export async function retry(id: string): Promise<FormState> {
  return attempt(() => redrive(id), "Sent back to be tried again.");
}

/** Give up on a dead letter. The event stays in the log; only its delivery is dropped. */
export async function drop(id: string, _: FormState, form: FormData): Promise<FormState> {
  if (form.get("sure") !== "1") return { error: "Tick the box to confirm." };
  return attempt(() => discard(id), "Dropped.");
}

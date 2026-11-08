"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { ApiError, completeTask } from "@/lib/api";
import { outputFrom, text, type FormState } from "@/lib/forms";

/** Do a task's work: the answer goes back to the run as its result (FR-HUM-08). AIOS checks
 * that this person is one it was assigned to, and that the answer fits what was asked. */
export async function complete(id: string, _: FormState, form: FormData): Promise<FormState> {
  const { output } = outputFrom(form);
  if (output === undefined) return { error: "Write the result first." };
  const comment = text(form, "comment");
  try {
    await completeTask(id, output, comment || undefined);
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  revalidatePath("/tasks");
  redirect("/tasks");
}

"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { ApiError, correctMemory, forgetMemory } from "@/lib/api";
import { text, type FormState } from "@/lib/forms";

/** Correct an item. It becomes a new version, and the change is on the audit record. */
export async function correct(id: string, version: number, _: FormState, form: FormData): Promise<FormState> {
  const content = text(form, "content");
  if (!content) return { error: "An item can't be empty. Forget it instead." };
  try {
    await correctMemory(id, content, version);
  } catch (error) {
    if (error instanceof ApiError) {
      return {
        error:
          error.status === 409
            ? "Someone changed it while you were editing. Reload to see their version."
            : error.message,
      };
    }
    throw error;
  }
  revalidatePath(`/memory/${id}`);
  return { done: "Saved as a new version." };
}

export async function forget(id: string, _: FormState, form: FormData): Promise<FormState> {
  if (form.get("sure") !== "1") return { error: "Tick the box to confirm. This can't be undone." };
  try {
    const result = await forgetMemory(id);
    if (result.held > 0) return { error: "It is under a legal hold, so it was kept." };
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  redirect("/memory");
}

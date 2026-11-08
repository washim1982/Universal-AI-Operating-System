"use server";

import { revalidatePath } from "next/cache";

import { ApiError, setContact } from "@/lib/api";
import { text, type FormState } from "@/lib/forms";

/** Where this person is told about approvals for them (FR-HUM-07). Only they can set it: an
 * address set by someone else would send their links to someone else. */
export async function saveContact(_: FormState, form: FormData): Promise<FormState> {
  const email = text(form, "email");
  const slack = text(form, "slack");
  try {
    await setContact({ email: email || null, slack: slack || null });
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  revalidatePath("/profile");
  return { done: email || slack ? "Saved. Approvals for you will reach you there." : "Removed." };
}

"use server";

import { revalidatePath } from "next/cache";

import { ApiError, lift, suspend } from "@/lib/api";
import { text, type FormState } from "@/lib/forms";

type Kind = "agent" | "app";

/** The kill switch (FR-SEC-13): it takes effect at the next call, and says why. */
export async function stop(kind: Kind, name: string, _: FormState, form: FormData): Promise<FormState> {
  const reason = text(form, "reason");
  if (!reason) return { error: "Say why. It is kept on the record." };
  try {
    await suspend(kind, name, reason);
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  revalidatePath("/agents");
  return { done: `${name} is stopped.` };
}

export async function resume(kind: Kind, name: string): Promise<FormState> {
  try {
    await lift(kind, name);
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  revalidatePath("/agents");
  return { done: `${name} may work again.` };
}

"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { answer, ApiError } from "@/lib/api";

export interface DecisionState {
  error?: string;
}

/** Answer an approval. AIOS decides whether this caller may: the form only asks. */
export async function decide(
  id: string,
  _: DecisionState,
  form: FormData,
): Promise<DecisionState> {
  const verdict = form.get("verdict");
  if (verdict !== "approve" && verdict !== "deny") return { error: "Choose approve or deny." };
  const comment = String(form.get("comment") ?? "").trim();
  try {
    await answer(id, verdict, comment || undefined);
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  revalidatePath("/approvals");
  redirect("/approvals");
}

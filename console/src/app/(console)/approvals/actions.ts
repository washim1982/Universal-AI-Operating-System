"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { answer, ApiError, approval } from "@/lib/api";
import { editsFrom } from "@/lib/forms";

export interface DecisionState {
  error?: string;
}

/** Answer an approval. AIOS decides whether this caller may: the form only asks. What an
 * approver changed is compared with the approval as AIOS has it, not as the page showed it. */
export async function decide(
  id: string,
  _: DecisionState,
  form: FormData,
): Promise<DecisionState> {
  const verdict = form.get("verdict");
  if (verdict !== "approve" && verdict !== "deny") return { error: "Choose approve or deny." };
  const comment = String(form.get("comment") ?? "").trim();
  const changing = [...form.keys()].some((name) => name.startsWith("edit."));
  try {
    const edits =
      verdict === "approve" && changing ? editsFrom(form, (await approval(id)).payload) : undefined;
    if (edits) await answer(id, verdict, comment || undefined, edits);
    else await answer(id, verdict, comment || undefined);
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  revalidatePath("/approvals");
  redirect("/approvals");
}

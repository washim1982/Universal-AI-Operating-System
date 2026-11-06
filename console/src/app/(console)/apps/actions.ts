"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { ApiError, configure, consent, refuse, rollback, uninstall } from "@/lib/api";
import { grantsFrom, text, type FormState } from "@/lib/forms";

async function attempt(app: string, act: () => Promise<unknown>, done: string): Promise<FormState> {
  try {
    await act();
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
  revalidatePath(`/apps/${app}`);
  return { done };
}

export async function agree(app: string, _: FormState, form: FormData): Promise<FormState> {
  const grants = grantsFrom(form);
  return attempt(app, () => consent(app, grants), "Agreed. What you left out is refused.");
}

export async function refuseVersion(app: string): Promise<FormState> {
  return attempt(app, () => refuse(app), "Refused. What was running keeps running.");
}

export async function saveConfig(app: string, _: FormState, form: FormData): Promise<FormState> {
  let config: unknown;
  try {
    config = JSON.parse(text(form, "config") || "{}");
  } catch {
    return { error: "That isn't JSON." };
  }
  if (typeof config !== "object" || config === null || Array.isArray(config)) {
    return { error: "The configuration is a JSON object: { ... }." };
  }
  return attempt(app, () => configure(app, config as Record<string, unknown>), "Saved.");
}

export async function rollBack(app: string, _: FormState, form: FormData): Promise<FormState> {
  const version = text(form, "version");
  if (!version) return { error: "Choose a version." };
  return attempt(app, () => rollback(app, version), `Going back to ${version}.`);
}

/** Uninstall needs its name typed, and a choice about data: nothing goes by default. */
export async function remove(app: string, _: FormState, form: FormData): Promise<FormState> {
  const data = form.get("data");
  if (data !== "retain" && data !== "delete") return { error: "Say what happens to its data." };
  if (text(form, "confirm") !== app) return { error: `Type ${app} to confirm.` };
  const result = await attempt(app, () => uninstall(app, data), "Uninstalled.");
  if (result.error) return result;
  revalidatePath("/apps");
  redirect("/apps");
}

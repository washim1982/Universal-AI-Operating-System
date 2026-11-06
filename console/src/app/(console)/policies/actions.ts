"use server";

import { ApiError, simulate } from "@/lib/api";
import { text } from "@/lib/forms";
import type { SimulatedAction, Verdict } from "@/lib/types";

export interface SimulationState {
  verdict?: Verdict;
  error?: string;
}

const ACTIONS: SimulatedAction[] = ["tool.invoke", "memory.read", "memory.write", "task.dispatch", "model.route"];

/** Ask the policy engine. Nothing happens, and nothing is recorded. */
export async function ask(app: string, _: SimulationState, form: FormData): Promise<SimulationState> {
  const action = text(form, "would") as SimulatedAction;
  if (!ACTIONS.includes(action)) return { error: "Choose what the agent would do." };
  const agent = text(form, "agent");
  if (!agent) return { error: "Choose the agent." };
  let input: unknown = {};
  const raw = text(form, "input");
  if (raw) {
    try {
      input = JSON.parse(raw);
    } catch {
      return { error: "The tool's input isn't JSON." };
    }
    if (typeof input !== "object" || input === null || Array.isArray(input)) {
      return { error: "The tool's input is a JSON object: { ... }." };
    }
  }
  try {
    const verdict = await simulate(app, {
      action,
      agent,
      resource: text(form, "resource"),
      side_effect: text(form, "side_effect") || undefined,
      on_behalf_of: text(form, "on_behalf_of") || undefined,
      tainted: form.get("tainted") === "1",
      approved: form.get("approved") === "1",
      input: input as Record<string, unknown>,
    });
    return { verdict };
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }
}

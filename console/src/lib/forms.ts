import type { Changes } from "./api";
import type { Grants } from "./types";

/** What a console form shows after it is sent: what went wrong, or what happened. */
export interface FormState {
  error?: string;
  done?: string;
}

/** What the ticked boxes of a consent form grant. What isn't ticked is refused. */
export function grantsFrom(form: FormData): Grants {
  const all = (name: string) => form.getAll(name).map(String);
  return {
    tools: all("tool"),
    data_classes: all("data_class"),
    memory_scopes: all("memory_scope"),
    hosts: all("host"),
  };
}

/** The trimmed text of a field, or "" when there is none. */
export function text(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value.trim() : "";
}

/** JSON is read as JSON; anything else is taken as text, as the CLI does. */
function jsonOrText(found: string): unknown {
  try {
    return JSON.parse(found) as unknown;
  } catch {
    return found;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** How a parameter is shown to edit: text as it is, anything else as JSON. */
export function editable(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** What an approver changed (FR-HUM-06): each ``edit.<name>`` field that now differs from what
 * was asked. A parameter that was text stays text; anything else is read as JSON, or as text. */
export function editsFrom(form: FormData, asked: unknown): Record<string, unknown> | undefined {
  if (!isRecord(asked)) return undefined;
  const edits: Record<string, unknown> = {};
  for (const [name, original] of Object.entries(asked)) {
    const field = form.get(`edit.${name}`);
    if (typeof field !== "string") continue;
    // a browser sends a textarea's line breaks as CRLF, whatever was there
    const value = typeof original === "string" ? field.replace(/\r\n?/g, "\n") : jsonOrText(field.trim());
    if (JSON.stringify(value) !== JSON.stringify(original)) edits[name] = value;
  }
  return Object.keys(edits).length > 0 ? edits : undefined;
}

/** A task's answer, or a correction: JSON, or text. Nothing typed is none. */
export function outputFrom(form: FormData, name = "output"): { output?: unknown } {
  const found = text(form, name);
  return found ? { output: jsonOrText(found) } : {};
}

/** What a fork or retry form changes. An empty field keeps what the source run had. */
export function changesFrom(form: FormData): Changes {
  const changes: Changes = {};
  const step = text(form, "step");
  const input = text(form, "input");
  const stepInput = text(form, "step_input");
  const model = text(form, "model");
  if (step) changes.step = step;
  if (input) changes.input = jsonOrText(input);
  if (stepInput) changes.step_input = jsonOrText(stepInput);
  if (model) changes.model = model;
  return changes;
}

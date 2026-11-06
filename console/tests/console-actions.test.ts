import { beforeEach, describe, expect, it, vi } from "vitest";

import { resume, stop } from "@/app/(console)/agents/actions";
import { agree, refuseVersion, remove, rollBack, saveConfig } from "@/app/(console)/apps/actions";
import { drop, retry } from "@/app/(console)/events/actions";
import { correct, forget } from "@/app/(console)/memory/actions";
import { ask } from "@/app/(console)/policies/actions";
import { control, restart } from "@/app/(console)/runs/actions";
import * as api from "@/lib/api";
import { grantsFrom } from "@/lib/forms";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("next/navigation", () => ({
  redirect: vi.fn((to: string) => {
    throw new Error(`redirected to ${to}`);
  }),
}));
vi.mock("@/lib/api", async (original) => {
  const real = await original<typeof import("@/lib/api")>();
  const stubs = Object.fromEntries(
    [
      "consent", "refuse", "rollback", "configure", "uninstall", "suspend", "lift", "redrive",
      "discard", "correctMemory", "forgetMemory", "simulate", "controlRun",
    ].map((name) => [name, vi.fn(async () => ({}))]),
  );
  return { ...real, ...stubs, restartRun: vi.fn(async () => ({ run_id: "01NEW" })) };
});

function form(fields: Record<string, string | string[]>): FormData {
  const data = new FormData();
  for (const [name, value] of Object.entries(fields)) {
    for (const one of [value].flat()) data.append(name, one);
  }
  return data;
}

beforeEach(() => vi.clearAllMocks());

describe("consent", () => {
  it("grants exactly what is ticked", async () => {
    const ticked = form({ tool: ["a.b.c", "d.e.f"], data_class: "internal" });
    expect(grantsFrom(ticked)).toEqual({
      tools: ["a.b.c", "d.e.f"],
      data_classes: ["internal"],
      memory_scopes: [],
      hosts: [],
    });
    expect(await agree("desk", {}, ticked)).toEqual({ done: "Agreed. What you left out is refused." });
    expect(api.consent).toHaveBeenCalledWith("desk", grantsFrom(ticked));
  });

  it("ticking nothing grants nothing, and says so to AIOS", async () => {
    await agree("desk", {}, form({}));
    expect(api.consent).toHaveBeenCalledWith("desk", { tools: [], data_classes: [], memory_scopes: [], hosts: [] });
  });

  it("passes on what AIOS said when it refuses", async () => {
    vi.mocked(api.consent).mockRejectedValueOnce(new api.ApiError(422, "didn't ask for that"));
    expect(await agree("desk", {}, form({ tool: "x.y.z" }))).toEqual({ error: "didn't ask for that" });
  });

  it("refusing a version refuses it", async () => {
    await refuseVersion("desk");
    expect(api.refuse).toHaveBeenCalledWith("desk");
  });
});

describe("configuration and versions", () => {
  it("sends a JSON object", async () => {
    await saveConfig("desk", {}, form({ config: '{"greeting": "hi"}' }));
    expect(api.configure).toHaveBeenCalledWith("desk", { greeting: "hi" });
  });

  it.each(["not json", "[1, 2]", "5", "null"])("refuses %s before sending it", async (config) => {
    expect((await saveConfig("desk", {}, form({ config }))).error).toBeTruthy();
    expect(api.configure).not.toHaveBeenCalled();
  });

  it("rolls back to the version chosen", async () => {
    await rollBack("desk", {}, form({ version: "1.0.0" }));
    expect(api.rollback).toHaveBeenCalledWith("desk", "1.0.0");
    expect((await rollBack("desk", {}, form({}))).error).toBe("Choose a version.");
  });
});

describe("uninstalling", () => {
  it("needs a choice about data", async () => {
    expect(await remove("desk", {}, form({ confirm: "desk" }))).toEqual({ error: "Say what happens to its data." });
    expect(api.uninstall).not.toHaveBeenCalled();
  });

  it("needs the name typed", async () => {
    expect((await remove("desk", {}, form({ data: "delete", confirm: "Desk" }))).error).toBe("Type desk to confirm.");
    expect(api.uninstall).not.toHaveBeenCalled();
  });

  it("then goes, with the choice made", async () => {
    await expect(remove("desk", {}, form({ data: "retain", confirm: "desk" }))).rejects.toThrow("redirected to /apps");
    expect(api.uninstall).toHaveBeenCalledWith("desk", "retain");
  });
});

describe("the kill switch", () => {
  it("needs a reason", async () => {
    expect((await stop("agent", "writer", {}, form({ reason: "  " }))).error).toBeTruthy();
    expect(api.suspend).not.toHaveBeenCalled();
  });

  it("stops and restarts", async () => {
    await stop("app", "desk", {}, form({ reason: "audit" }));
    expect(api.suspend).toHaveBeenCalledWith("app", "desk", "audit");
    await resume("app", "desk");
    expect(api.lift).toHaveBeenCalledWith("app", "desk");
  });
});

describe("memory", () => {
  it("sends the correction with the version it was made from", async () => {
    await correct("01M", 3, {}, form({ content: " fixed " }));
    expect(api.correctMemory).toHaveBeenCalledWith("01M", "fixed", 3);
  });

  it("explains a clash in words", async () => {
    vi.mocked(api.correctMemory).mockRejectedValueOnce(new api.ApiError(409, "version 4, not 3"));
    expect((await correct("01M", 3, {}, form({ content: "x" }))).error).toMatch(/Someone changed it/);
  });

  it("won't save an empty item", async () => {
    expect((await correct("01M", 3, {}, form({ content: "" }))).error).toBeTruthy();
    expect(api.correctMemory).not.toHaveBeenCalled();
  });

  it("forgets only when told it is understood", async () => {
    expect((await forget("01M", {}, form({}))).error).toBeTruthy();
    expect(api.forgetMemory).not.toHaveBeenCalled();
    await expect(forget("01M", {}, form({ sure: "1" }))).rejects.toThrow("redirected to /memory");
  });

  it("says when a legal hold kept it", async () => {
    vi.mocked(api.forgetMemory).mockResolvedValueOnce({ erased: 0, held: 1 });
    expect((await forget("01M", {}, form({ sure: "1" }))).error).toMatch(/legal hold/);
  });
});

describe("dead letters", () => {
  it("retries, and drops only when sure", async () => {
    await retry("01D");
    expect(api.redrive).toHaveBeenCalledWith("01D");
    expect((await drop("01D", {}, form({}))).error).toBeTruthy();
    expect(api.discard).not.toHaveBeenCalled();
    await drop("01D", {}, form({ sure: "1" }));
    expect(api.discard).toHaveBeenCalledWith("01D");
  });
});

describe("the simulator", () => {
  it("passes the question on", async () => {
    vi.mocked(api.simulate).mockResolvedValueOnce({ effect: "allow", policies: ["p"], approvers: [], reason: "" });
    const state = await ask(
      "desk",
      {},
      form({ would: "tool.invoke", agent: "refunder", resource: "pay.refund.create", input: '{"amountUSD": 120}', tainted: "1" }),
    );
    expect(state.verdict?.effect).toBe("allow");
    expect(api.simulate).toHaveBeenCalledWith("desk", {
      action: "tool.invoke",
      agent: "refunder",
      resource: "pay.refund.create",
      side_effect: undefined,
      on_behalf_of: undefined,
      tainted: true,
      approved: false,
      input: { amountUSD: 120 },
    });
  });

  it.each([
    [{ would: "rm -rf", agent: "a" }, "Choose what the agent would do."],
    [{ would: "model.route", agent: "" }, "Choose the agent."],
    [{ would: "tool.invoke", agent: "a", input: "{oops" }, "The tool's input isn't JSON."],
  ])("refuses %j", async (fields, error) => {
    expect((await ask("desk", {}, form(fields))).error).toBe(error);
    expect(api.simulate).not.toHaveBeenCalled();
  });
});

describe("running a finished run again", () => {
  it("replays with nothing changed, and goes to the new run", async () => {
    const sent = form({ mode: "replay", step: "pay", input: "ignored" });
    await expect(restart("a/b", "01R", {}, sent)).rejects.toThrow("redirected to /runs/a%2Fb/01NEW");
    expect(api.restartRun).toHaveBeenCalledWith("a/b", "01R", "replay", {});
  });

  it("forks with what was changed: JSON as JSON, the rest as text", async () => {
    const sent = form({ mode: "fork", step: "pay", input: '{"item": "pear"}', step_input: "be brief", model: "" });
    await expect(restart("shop", "01R", {}, sent)).rejects.toThrow("redirected");
    expect(api.restartRun).toHaveBeenCalledWith("shop", "01R", "fork", {
      step: "pay",
      input: { item: "pear" },
      step_input: "be brief",
    });
  });

  it("needs a step to fork from", async () => {
    expect((await restart("shop", "01R", {}, form({ mode: "fork", step: "" }))).error).toBeTruthy();
    expect(api.restartRun).not.toHaveBeenCalled();
  });

  it("retries only once the box is ticked", async () => {
    expect((await restart("shop", "01R", {}, form({ mode: "retry" }))).error).toContain("for real");
    expect(api.restartRun).not.toHaveBeenCalled();
    await expect(restart("shop", "01R", {}, form({ mode: "retry", really: "on" }))).rejects.toThrow("redirected");
    expect(api.restartRun).toHaveBeenCalledWith("shop", "01R", "retry", {});
  });

  it("passes on what AIOS said, and stays put", async () => {
    vi.mocked(api.restartRun).mockRejectedValueOnce(new api.ApiError(403, "you may not control runs"));
    const said = await restart("shop", "01R", {}, form({ mode: "retry", really: "on" }));
    expect(said).toEqual({ error: "you may not control runs" });
  });

  it("refuses anything else", async () => {
    expect((await restart("shop", "01R", {}, form({ mode: "delete" }))).error).toBeTruthy();
  });
});

describe("run controls", () => {
  it("only pause, resume or cancel", async () => {
    expect((await control("desk", "01R", {}, form({ control: "delete" }))).error).toBeTruthy();
    await control("desk", "01R", {}, form({ control: "cancel" }));
    expect(api.controlRun).toHaveBeenCalledWith("desk", "01R", "cancel");
  });
});

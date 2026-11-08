// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { decide } from "@/app/(console)/approvals/actions";
import { feedback } from "@/app/(console)/runs/actions";
import { complete } from "@/app/(console)/tasks/actions";
import { ApprovalDetails } from "@/components/ApprovalDetails";
import { DecisionForm } from "@/components/DecisionForm";
import { answer, ApiError, approval, completeTask, giveFeedback } from "@/lib/api";
import { editable, editsFrom, outputFrom } from "@/lib/forms";
import type { Approval } from "@/lib/types";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("next/navigation", () => ({
  redirect: vi.fn((to: string) => {
    throw new Error(`redirected to ${to}`);
  }),
}));
vi.mock("@/lib/session", () => ({ session: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  answer: vi.fn(),
  approval: vi.fn(),
  completeTask: vi.fn(),
  giveFeedback: vi.fn(),
}));

const APPROVAL: Approval = {
  approval: "01HZY",
  app: "desk",
  run: "01RUN",
  status: "pending",
  summary: 'pay.refund.create(amountUSD=120, order="1001")',
  action: "pay.refund.create",
  payload: { amountUSD: 120, order: "1001" },
  risk: "high",
  policies: ["refunds/approval/0"],
  rationale: null,
  tainted: false,
  taint: [],
  requested_by: "agent:refund-agent@acme",
  initiator: "user:alice@acme",
  requested_at: "2026-11-01T10:00:00+00:00",
  expires_at: "2026-11-02T10:00:00+00:00",
  data_class: "internal",
  by: null,
  comment: null,
  decided_at: null,
  quorum: 1,
  votes: [],
  approvers: ["role:lead"],
  level: 0,
  preview: null,
  edits: null,
};

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [name, value] of Object.entries(fields)) data.set(name, value);
  return data;
}

afterEach(cleanup);
beforeEach(() => {
  for (const mocked of [answer, approval, completeTask, giveFeedback]) vi.mocked(mocked).mockReset();
});

describe("changing what is approved (FR-HUM-06)", () => {
  it("sends only what changed, text as text and numbers as numbers", () => {
    const asked = { amountUSD: 120, order: "1001", note: "late" };
    const edited = form({ "edit.amountUSD": " 60 ", "edit.order": "1001", "edit.note": "late" });
    expect(editsFrom(edited, asked)).toEqual({ amountUSD: 60 });
    expect(editsFrom(form({ "edit.order": "1002" }), asked)).toEqual({ order: "1002" });
    expect(editsFrom(form({ "edit.amountUSD": "120" }), asked)).toBeUndefined();
    expect(editsFrom(form({ "edit.account": "mine" }), asked)).toBeUndefined(); // not asked
    expect(editsFrom(form({ "edit.x": "1" }), ["not", "named"])).toBeUndefined();
  });

  it("keeps line breaks, as a browser sends them, from counting as a change", () => {
    const asked = { content: "refunded $120\nfor order 1001\n" };
    expect(editsFrom(form({ "edit.content": "refunded $120\r\nfor order 1001\r\n" }), asked)).toBeUndefined();
    render(<DecisionForm action={vi.fn()} changeable={asked} />);
    const field = document.querySelector('[name="edit.content"]');
    expect(field?.tagName).toBe("TEXTAREA"); // an input would drop the line breaks
  });

  it("shows each parameter as it can be typed back", () => {
    expect(editable("1001")).toBe("1001");
    expect(editable(120)).toBe("120");
    expect(editable({ a: 1 })).toBe('{"a":1}');
  });

  it("compares with the approval as AIOS has it", async () => {
    vi.mocked(approval).mockResolvedValue({ ...APPROVAL, payload: { amountUSD: 120, order: "1001" } });
    const sent = form({ verdict: "approve", "edit.amountUSD": "60", "edit.order": "1001" });
    await expect(decide("01HZY", {}, sent)).rejects.toThrow("redirected to /approvals");
    expect(answer).toHaveBeenCalledWith("01HZY", "approve", undefined, { amountUSD: 60 });
  });

  it("changes nothing with a no", async () => {
    await expect(decide("01HZY", {}, form({ verdict: "deny", "edit.amountUSD": "60" }))).rejects.toThrow();
    expect(approval).not.toHaveBeenCalled();
    expect(answer).toHaveBeenCalledWith("01HZY", "deny", undefined);
  });

  it("says why AIOS refused the change", async () => {
    vi.mocked(approval).mockResolvedValue(APPROVAL);
    vi.mocked(answer).mockRejectedValue(new ApiError(422, "the action has no account"));
    const sent = form({ verdict: "approve", "edit.amountUSD": "60" });
    await expect(decide("01HZY", {}, sent)).resolves.toEqual({ error: "the action has no account" });
  });

  it("offers the fields to change, and only when one approver decides", () => {
    render(<DecisionForm action={vi.fn()} changeable={{ amountUSD: 120, order: "1001" }} />);
    expect(screen.getByText("Change something before approving")).toBeTruthy();
    expect((screen.getByDisplayValue("120") as HTMLInputElement).name).toBe("edit.amountUSD");
    cleanup();
    render(<DecisionForm action={vi.fn()} />);
    expect(screen.queryByText("Change something before approving")).toBeNull();
  });
});

describe("what an approver sees now", () => {
  it("shows the tool's dry run", () => {
    render(<ApprovalDetails approval={{ ...APPROVAL, preview: "would refund $120.00 for order 1001" }} />);
    expect(screen.getByText("What the tool says it would do")).toBeTruthy();
    expect(screen.getByText("would refund $120.00 for order 1001")).toBeTruthy();
  });

  it("shows how far a quorum has got, and who can answer", () => {
    const voted = { ...APPROVAL, quorum: 2, votes: [{ by: "user:ann@acme", comment: "checked" }] };
    render(<ApprovalDetails approval={{ ...voted, approvers: ["group:finance", "role:cfo"], level: 1 }} />);
    expect(screen.getByText("1 of 2 approvals")).toBeTruthy();
    expect(screen.getByText("ann: checked")).toBeTruthy();
    expect(screen.getByText(/group:finance, role:cfo/)).toBeTruthy();
    expect(screen.getByText(/escalated/)).toBeTruthy();
  });

  it("shows what was changed before it was approved", () => {
    render(<ApprovalDetails approval={{ ...APPROVAL, status: "granted", edits: { amountUSD: 60 } }} />);
    expect(screen.getByText("Changed before it was approved")).toBeTruthy();
    expect(screen.getByText(/"amountUSD": 60/)).toBeTruthy();
  });
});

describe("work for people (FR-HUM-08)", () => {
  it("sends the result as JSON, or as text", async () => {
    expect(outputFrom(form({ output: ' {"verdict": "sign"} ' }))).toEqual({ output: { verdict: "sign" } });
    expect(outputFrom(form({ output: "looks fine" }))).toEqual({ output: "looks fine" });
    expect(outputFrom(form({ output: "  " }))).toEqual({});
    await expect(complete("t1", {}, form({ output: '{"verdict":"sign"}', comment: "ok" }))).rejects.toThrow(
      "redirected to /tasks",
    );
    expect(completeTask).toHaveBeenCalledWith("t1", { verdict: "sign" }, "ok");
  });

  it("needs a result, and says why AIOS refused one", async () => {
    await expect(complete("t1", {}, form({ output: "" }))).resolves.toEqual({ error: "Write the result first." });
    vi.mocked(completeTask).mockRejectedValue(new ApiError(422, "the answer doesn't fit"));
    await expect(complete("t1", {}, form({ output: "maybe" }))).resolves.toEqual({
      error: "the answer doesn't fit",
    });
  });
});

describe("feedback (FR-HUM-09)", () => {
  it("sends a rating, a correction and whether to remember it", async () => {
    vi.mocked(giveFeedback).mockResolvedValue({ memory: "m1" } as never);
    const sent = form({ rating: "bad", step: "", correction: '{"answer":"France"}', remember: "on" });
    await expect(feedback("desk", "r1", {}, sent)).resolves.toEqual({
      done: "Thank you. The App's agents can find it now.",
    });
    expect(giveFeedback).toHaveBeenCalledWith("desk", "r1", {
      step: null,
      rating: "bad",
      correction: { answer: "France" },
      comment: null,
      remember: true,
    });
  });

  it("needs something to say", async () => {
    await expect(feedback("desk", "r1", {}, form({ rating: "meh" }))).resolves.toEqual({
      error: "Give a rating, a correction or a comment.",
    });
    expect(giveFeedback).not.toHaveBeenCalled();
  });
});

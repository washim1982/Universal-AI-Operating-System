// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ApprovalDetails } from "@/components/ApprovalDetails";
import { ApprovalList } from "@/components/ApprovalList";
import type { Approval } from "@/lib/types";

const APPROVAL: Approval = {
  approval: "01HZY",
  app: "desk",
  run: "01RUN",
  status: "pending",
  summary: "pay.refund.create(amountUSD=120)",
  action: "pay.refund.create",
  payload: { amountUSD: 120 },
  risk: "high",
  policies: ["refunds/approval/0"],
  rationale: "It arrived broken.",
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
};

afterEach(cleanup);

describe("what an approver sees", () => {
  it("shows exactly what would happen", () => {
    render(<ApprovalDetails approval={APPROVAL} />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(APPROVAL.summary);
    expect(screen.getByText(/"amountUSD": 120/)).toBeTruthy();
    expect(screen.getByText("pay.refund.create")).toBeTruthy();
    expect(screen.getByText("high risk")).toBeTruthy();
  });

  it("says the rationale is only the agent's claim", () => {
    render(<ApprovalDetails approval={APPROVAL} />);
    expect(screen.getByText("It arrived broken.")).toBeTruthy();
    expect(screen.getByText(/A claim, not a fact/)).toBeTruthy();
  });

  it("names the rule, the person and what silence means", () => {
    render(<ApprovalDetails approval={APPROVAL} />);
    expect(screen.getByText("refunds/approval/0")).toBeTruthy();
    expect(screen.getByText("alice")).toBeTruthy();
    expect(screen.getByText(/If nobody answers, the answer is no/)).toBeTruthy();
  });

  it("links to the run", () => {
    render(<ApprovalDetails approval={APPROVAL} />);
    expect(screen.getByRole("link", { name: "01RUN" }).getAttribute("href")).toBe(
      "/runs/desk/01RUN",
    );
  });

  it("warns when untrusted content reached the agent, and says from where", () => {
    const taint = ["memory:01ITEM", "tool:web.page.read", "suspected:memory"];
    render(<ApprovalDetails approval={{ ...APPROVAL, tainted: true, taint }} />);
    const notice = screen.getByRole("note");
    expect(notice.textContent).toContain("read untrusted content");
    const items = within(notice).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "a stored document (01ITEM)",
      "the tool web.page.read",
      "what looks like an injection, in memory",
    ]);
  });

  it("doesn't warn when nothing untrusted was read", () => {
    render(<ApprovalDetails approval={APPROVAL} />);
    expect(screen.queryByRole("note")).toBeNull();
  });

  it.each([
    ["summary", { summary: '<img src=x onerror="alert(1)">' }],
    ["rationale", { rationale: "<script>alert(1)</script>" }],
    ["payload", { payload: { note: "<img src=x onerror=alert(1)>" } }],
    ["a taint source", { tainted: true, taint: ["tool:<b>bold</b>"] }],
  ])("shows markup in %s as text, because an attacker may have written it", (_, change) => {
    const { container } = render(<ApprovalDetails approval={{ ...APPROVAL, ...change }} />);
    expect(container.querySelector("img, script, b")).toBeNull();
  });

  it("shows a long unbroken string without any markup either", () => {
    const summary = "a".repeat(5000);
    render(<ApprovalDetails approval={{ ...APPROVAL, summary }} />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toHaveLength(5000);
  });
});

describe("the inbox", () => {
  it("says when there is nothing to do", () => {
    render(<ApprovalList approvals={[]} />);
    expect(screen.getByText("Nothing is waiting for you.")).toBeTruthy();
  });

  it("leads with what would happen, and flags untrusted content", () => {
    render(<ApprovalList approvals={[{ ...APPROVAL, tainted: true }]} />);
    const link = screen.getByRole("link");
    expect(link.getAttribute("href")).toBe("/approvals/01HZY");
    expect(link.textContent).toContain(APPROVAL.summary);
    expect(link.textContent).toContain("untrusted content");
    expect(link.textContent).toContain("for alice");
  });

  it("doesn't flag what is clean", () => {
    render(<ApprovalList approvals={[APPROVAL]} />);
    expect(screen.queryByText("untrusted content")).toBeNull();
  });
});

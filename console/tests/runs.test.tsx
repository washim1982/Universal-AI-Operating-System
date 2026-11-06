// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { RunDetailsView } from "@/components/RunDetailsView";
import { RunTable } from "@/components/RunTable";
import type { Run, RunDetails } from "@/lib/types";

const RUN: Run = {
  run_id: "01RUN",
  app: "desk",
  status: "paused",
  workflow: "ticket",
  started_at: "2026-11-01T10:00:00+00:00",
  updated_at: "2026-11-01T10:00:05+00:00",
  on_behalf_of: "user:alice@acme",
  tainted: false,
  cost_usd: 0.0146,
};

const DETAILS: RunDetails = {
  run_id: "01RUN",
  app: "desk",
  status: "failed",
  waiting: [],
  tainted: false,
  taint: [],
  steps: { research: "completed", resolve: "failed" },
  output: null,
  usage: { cost_usd: 0.5 },
  error: { code: "budget.exceeded", message: "tenant cost budget exceeded" },
  events: 12,
};

afterEach(cleanup);

describe("the run list", () => {
  it("says when there are none", () => {
    render(<RunTable runs={[]} />);
    expect(screen.getByText("No runs yet.")).toBeTruthy();
  });

  it("shows each run, and links to it", () => {
    render(<RunTable runs={[RUN]} />);
    expect(screen.getByText("paused").closest("a")?.getAttribute("href")).toBe("/runs/desk/01RUN");
    expect(screen.getByText("ticket")).toBeTruthy();
    expect(screen.getByText("alice")).toBeTruthy();
    expect(screen.getByText("$0.0146")).toBeTruthy();
  });

  it("marks a run that read something untrusted", () => {
    render(<RunTable runs={[{ ...RUN, tainted: true }]} />);
    expect(screen.getByText("untrusted")).toBeTruthy();
  });

  it("escapes what goes in an address", () => {
    render(<RunTable runs={[{ ...RUN, app: "a/b" }]} />);
    expect(screen.getByText("paused").closest("a")?.getAttribute("href")).toBe("/runs/a%2Fb/01RUN");
  });
});

describe("one run", () => {
  const events = [{ id: "e1", type: "run.started", time: "2026-11-01T10:00:00+00:00" }];

  it("says why it failed", () => {
    render(<RunDetailsView run={DETAILS} events={events} />);
    expect(screen.getByRole("alert").textContent).toBe(
      "budget.exceeded: tenant cost budget exceeded",
    );
  });

  it("lists its steps and what happened", () => {
    render(<RunDetailsView run={DETAILS} events={events} />);
    expect(screen.getByText("research")).toBeTruthy();
    expect(screen.getByText("run.started")).toBeTruthy();
    expect(screen.getByText("$0.5000")).toBeTruthy();
  });

  it("says what it is waiting for", () => {
    render(
      <RunDetailsView
        run={{ ...DETAILS, status: "paused", error: null, waiting: ["approval:refund"] }}
        events={events}
      />,
    );
    expect(screen.getByText("Waiting for approval:refund.")).toBeTruthy();
  });

  it("warns when it read something untrusted", () => {
    render(<RunDetailsView run={{ ...DETAILS, tainted: true, taint: ["input"] }} events={events} />);
    expect(screen.getByRole("note").textContent).toContain("the request");
  });

  it("shows output as text", () => {
    const output = { reply: "<img src=x onerror=alert(1)>" };
    const { container } = render(
      <RunDetailsView run={{ ...DETAILS, output }} events={events} />,
    );
    expect(container.querySelector("img")).toBeNull();
  });
});

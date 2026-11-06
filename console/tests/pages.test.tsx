// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BudgetBar } from "@/components/BudgetBar";
import { ConsentForm } from "@/components/ConsentForm";
import { EventTable } from "@/components/EventTable";
import { Forbidden } from "@/components/Forbidden";
import { RunControls } from "@/components/RunControls";
import { SpendTable } from "@/components/SpendTable";
import type { Budget, Requested } from "@/lib/types";

vi.mock("@/app/(console)/apps/actions", () => ({ agree: vi.fn(), refuseVersion: vi.fn() }));
vi.mock("@/app/(console)/runs/actions", () => ({ control: vi.fn() }));

afterEach(cleanup);

const REQUESTED: Requested = {
  tools: ["mail.message.read", "mail.message.send"],
  data_classes: ["internal"],
  memory_scopes: ["app"],
  hosts: [],
  per_run_usd: 0.5,
  monthly_usd: null,
  starts: ["chat", "schedule: 0 9 * * *"],
};
const NOTHING = { tools: [], data_classes: [], memory_scopes: [], hosts: [] };

describe("the consent step", () => {
  it("lists what is asked, ticked", () => {
    render(<ConsentForm app="desk" version="1.1.0" requested={REQUESTED} refused={NOTHING} />);
    const read = screen.getByRole("checkbox", { name: /mail.message.read/ }) as HTMLInputElement;
    expect(read.checked).toBe(true);
    expect(screen.getByRole("checkbox", { name: /internal/ })).toBeTruthy();
    expect(screen.queryByText("Hosts it may reach")).toBeNull();
  });

  it("leaves what was refused before unticked, and says so", () => {
    const refused = { ...NOTHING, tools: ["mail.message.send"] };
    render(<ConsentForm app="desk" version="1.1.0" requested={REQUESTED} refused={refused} />);
    const send = screen.getByRole("checkbox", { name: /mail.message.send/ }) as HTMLInputElement;
    expect(send.checked).toBe(false);
    expect(screen.getByText("refused before")).toBeTruthy();
  });

  it("says when runs start without anyone signed in", () => {
    const starts = ["chat", "schedule: 0 9 * * *", "webhook: orders"];
    render(<ConsentForm app="desk" version="1.1.0" requested={{ ...REQUESTED, starts }} refused={NOTHING} />);
    expect(screen.getByText(/A schedule starts runs by itself/)).toBeTruthy();
    expect(screen.getByText(/A webhook starts runs for whoever holds its secret/)).toBeTruthy();
    expect(screen.getByText(/\$0.5000 a run/)).toBeTruthy();
  });
});

const BUDGET: Budget = { level: "app", subject: "app:desk:2026-11", limit_usd: 10, soft_usd: 8, spent_usd: 9 };

describe("a budget", () => {
  it("says how far through it is, and where soft starts", () => {
    render(<BudgetBar budget={BUDGET} />);
    const meter = screen.getByRole("meter");
    expect(meter.getAttribute("aria-valuenow")).toBe("90");
    expect(meter.getAttribute("aria-valuetext")).toBe("90% spent; the soft limit is at 80%");
    expect(meter.className).toContain("warning");
  });

  it("is red at the limit, and never runs past its end", () => {
    render(<BudgetBar budget={{ ...BUDGET, spent_usd: 12 }} />);
    const meter = screen.getByRole("meter");
    expect(meter.getAttribute("aria-valuenow")).toBe("100");
    expect(meter.className).toContain("danger");
  });
});

describe("spending", () => {
  it("heads its first column with what it lists", () => {
    render(<SpendTable title="By agent" column="Agent" rows={[]} />);
    expect(screen.queryByText("agent")).toBeNull();
  });

  it("names people the short way", () => {
    const rows = [{ key: "user:alice@acme", cost_usd: 1, calls: 2, input_tokens: 3, output_tokens: 4 }];
    render(<SpendTable title="By person" column="Person" rows={rows} people />);
    expect(screen.getByText("alice")).toBeTruthy();
  });

  it("says when nothing was spent", () => {
    render(<SpendTable title="By App" column="App" rows={[]} />);
    expect(screen.getByText("Nothing spent.")).toBeTruthy();
  });
});

describe("events", () => {
  it("links an event to its run, and marks untrusted ones", () => {
    const event = {
      position: 7,
      id: "01E",
      type: "tool.call.completed",
      time: "2026-11-02T10:00:00+00:00",
      source: "aios://acme/apps/desk",
      appid: "desk",
      tenantid: "acme",
      runid: "01RUN",
      principal: "agent:a@acme",
      dataclass: "internal",
      tainted: true,
      data: { tool: "web.page.read" },
    };
    render(<EventTable events={[event]} />);
    expect(screen.getByRole("link", { name: "run" }).getAttribute("href")).toBe("/runs/desk/01RUN");
    expect(screen.getByText("untrusted")).toBeTruthy();
  });
});

describe("a page beyond someone's roles", () => {
  it("says so, and what AIOS said", () => {
    render(<Forbidden what="seeing costs" detail="needs event.read" />);
    expect(screen.getByText(/Your roles don't include seeing costs/)).toBeTruthy();
    expect(screen.getByText("AIOS said: needs event.read")).toBeTruthy();
  });
});

describe("run controls", () => {
  it("offers pause to a run waiting for a person, not resume", () => {
    render(<RunControls app="desk" id="01R" status="paused" waiting={["approval:issue"]} />);
    expect(screen.getByRole("button", { name: "Pause" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
  });

  it("offers resume only to a run someone paused", () => {
    render(<RunControls app="desk" id="01R" status="paused" waiting={["request"]} />);
    expect(screen.getByRole("button", { name: "Resume" })).toBeTruthy();
  });

  it("offers nothing to a finished run", () => {
    const { container } = render(<RunControls app="desk" id="01R" status="completed" waiting={[]} />);
    expect(container.innerHTML).toBe("");
  });
});

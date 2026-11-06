// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActionForm } from "@/components/ActionForm";
import { ComparisonView } from "@/components/ComparisonView";
import { RestartForm } from "@/components/RestartForm";
import { RunDetailsView } from "@/components/RunDetailsView";
import { RunTable } from "@/components/RunTable";
import { StepDetails } from "@/components/StepDetails";
import type { FormState } from "@/lib/forms";
import type { Comparison, Run, RunDetails, Side, TraceNode } from "@/lib/types";

vi.mock("@/app/(console)/runs/actions", () => ({ restart: vi.fn() }));

afterEach(cleanup);

const STEPS = ["check", "pay", "tell"];

describe("running a finished run again", () => {
  it("is offered only once a run has finished", () => {
    for (const status of ["running", "paused"] as const) {
      const { container } = render(
        <RestartForm app="shop" id="01R" status={status} steps={STEPS} failed={null} />,
      );
      expect(container.innerHTML).toBe("");
    }
  });

  it("offers a replay, a fork and a retry", () => {
    render(<RestartForm app="shop" id="01R" status="completed" steps={STEPS} failed={null} />);
    expect(screen.getByRole("button", { name: "Replay" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Fork" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry for real" })).toBeTruthy();
    // A retry does things for real: the box isn't ticked for anyone.
    expect(screen.getByRole<HTMLInputElement>("checkbox").checked).toBe(false);
  });

  it("starts a retry where the run failed", () => {
    render(<RestartForm app="shop" id="01R" status="failed" steps={STEPS} failed="pay" />);
    const step = screen.getByRole<HTMLSelectElement>("combobox", { name: "From step" });
    expect(step.value).toBe("pay");
    expect(within(step).getByText("Where it failed (pay)")).toBeTruthy();
  });

  it("forks from the first step when nothing failed", () => {
    render(<RestartForm app="shop" id="01R" status="completed" steps={STEPS} failed={null} />);
    expect(screen.getByRole<HTMLSelectElement>("combobox", { name: "From step" }).value).toBe("check");
  });
});

describe("a form that keeps what was typed", () => {
  it("sends the button pressed, and leaves the fields as they were", async () => {
    const act = vi.fn(async (_: FormState, form: FormData) => ({ error: `no ${String(form.get("mode"))}` }));
    render(
      <ActionForm action={act} keep>
        <input name="step_input" aria-label="Step input" />
        <button type="submit" name="mode" value="fork">
          Fork
        </button>
      </ActionForm>,
    );
    const field = screen.getByLabelText<HTMLInputElement>("Step input");
    fireEvent.change(field, { target: { value: "be brief" } });
    fireEvent.click(screen.getByRole("button", { name: "Fork" }));
    expect((await screen.findByRole("alert")).textContent).toBe("no fork");
    expect(field.value).toBe("be brief");
    expect(act).toHaveBeenCalledTimes(1);
  });
});

const DETAILS: RunDetails = {
  run_id: "01NEW",
  app: "shop",
  status: "completed",
  waiting: [],
  tainted: false,
  taint: [],
  steps: { check: "completed" },
  output: "ok",
  usage: { cost_usd: 0 },
  error: null,
  events: 9,
  source: { run: "01OLD", mode: "fork", step: "pay", model: "anthropic/claude-haiku-5-5", live: false, copied: ["check"] },
};

describe("a run made from another", () => {
  it("says what it came from, and links to both and the comparison", () => {
    render(<RunDetailsView run={DETAILS} events={[]} />);
    expect(screen.getByText(/A fork of/).textContent).toContain("from step pay with anthropic/claude-haiku-5-5");
    expect(screen.getByText("Side effects were held back.", { exact: false })).toBeTruthy();
    expect(screen.getByText("01OLD").closest("a")?.getAttribute("href")).toBe("/runs/shop/01OLD");
    expect(screen.getByText("Compare the two").getAttribute("href")).toBe("/runs/shop/01OLD/compare/01NEW");
  });

  it("says nothing of the kind for a run that wasn't", () => {
    render(<RunDetailsView run={{ ...DETAILS, source: null }} events={[]} />);
    expect(screen.queryByText("Compare the two")).toBeNull();
  });

  it("is marked in the run list", () => {
    const run: Run = {
      run_id: "01NEW", app: "shop", status: "completed", workflow: "order", started_at: null,
      updated_at: "2026-11-02T10:00:00+00:00", on_behalf_of: null, tainted: false, cost_usd: 0,
      source_run: "01OLD", mode: "replay",
    };
    render(<RunTable runs={[run]} />);
    expect(screen.getByText("replay").getAttribute("title")).toBe("A replay of 01OLD");
  });
});

function node(fields: Partial<TraceNode>): TraceNode {
  return {
    id: "pay", type: "task", needs: [], status: "completed", attempts: 1, capability: "shop.pay",
    tool: null, started_at: null, ended_at: null, duration_ms: null, input: null, output: null,
    error: null, input_tokens: 0, output_tokens: 0, cost_usd: 0, tainted: false,
    model_calls: [], tool_calls: [], decisions: [], memory: [], approvals: [], ...fields,
  };
}

describe("a step of a replay or fork", () => {
  it("says it was kept, not run", () => {
    render(<StepDetails step={node({ copied: true })} />);
    expect(screen.getByText(/didn.t run again/)).toBeTruthy();
  });

  it("marks calls answered from the record, and side effects held back", () => {
    const at = "2026-11-02T10:00:00+00:00";
    render(
      <StepDetails
        step={node({
          model_calls: [
            { provider: "anthropic", model: "claude-sonnet-5-5", input_tokens: 10, output_tokens: 5,
              cached_tokens: 0, cost_usd: 0, latency_ms: 0, at, replayed: true },
          ],
          tool_calls: [
            { tool: "shop.order.place", status: "completed", latency_ms: 0, arguments: {}, error: null, at, stubbed: true },
          ],
        })}
      />,
    );
    expect(screen.getByText("replayed")).toBeTruthy();
    expect(screen.getByText("stubbed").getAttribute("title")).toContain("held back");
  });
});

function side(fields: Partial<Side> = {}): Side {
  return {
    status: "completed", output: "x", cost_usd: 0.01, input_tokens: 1, output_tokens: 1,
    duration_ms: 10, copied: false, stubbed: 0, error: null, ...fields,
  };
}

const COMPARED: Comparison = {
  a: "01OLD",
  b: "01NEW",
  status: ["completed", "completed"],
  output: ["told", "told"],
  same_output: true,
  cost_usd: [0.03, 0],
  steps: [
    { step: "check", a: side(), b: side({ copied: true, cost_usd: 0 }), same_output: true, same_status: true },
    {
      step: "pay",
      a: side({ output: { receipt: "R1" } }),
      b: side({ output: { stubbed: true, tool: "<b>shop</b>" }, stubbed: 1, cost_usd: 0 }),
      same_output: false,
      same_status: true,
    },
  ],
};

describe("two runs side by side", () => {
  it("shows each step of both, and which differ", () => {
    render(<ComparisonView app="shop" comparison={COMPARED} />);
    expect(screen.getByRole("status").textContent).toBe("Both runs gave the same output.");
    const pay = screen.getByRole("row", { name: /pay/ });
    expect(within(pay).getByText("1 stubbed")).toBeTruthy();
    expect(within(pay).getByText("different")).toBeTruthy();
    expect(within(screen.getByRole("row", { name: /check/ })).getByText("copied")).toBeTruthy();
    expect(screen.getByText("pay", { selector: "summary code" })).toBeTruthy();
  });

  it("links to both runs", () => {
    render(<ComparisonView app="a/b" comparison={COMPARED} />);
    expect(screen.getByText("01OLD").closest("a")?.getAttribute("href")).toBe("/runs/a%2Fb/01OLD");
    expect(screen.getByText("01NEW").closest("a")?.getAttribute("href")).toBe("/runs/a%2Fb/01NEW");
  });

  it("says when the outputs differ", () => {
    render(<ComparisonView app="shop" comparison={{ ...COMPARED, same_output: false }} />);
    expect(screen.getByRole("status").textContent).toBe("The runs gave different outputs.");
  });

  it("shows outputs as text", () => {
    const { container } = render(<ComparisonView app="shop" comparison={COMPARED} />);
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByLabelText("pay in B").textContent).toContain("<b>shop</b>");
  });
});

import { describe, expect, it } from "vitest";

import { filterFrom, matches } from "@/lib/search";
import type { Run } from "@/lib/types";

const RUN: Run = {
  run_id: "01ABC",
  app: "desk",
  status: "failed",
  workflow: "ticket",
  started_at: "2026-11-02T10:00:00+00:00",
  updated_at: "2026-11-02T10:00:05+00:00",
  on_behalf_of: "user:alice@acme",
  tainted: true,
  cost_usd: 0.1,
};

describe("searching runs", () => {
  it("reads a filter from the address, keeping only what makes sense", () => {
    expect(filterFrom({ app: "desk", status: "nonsense", q: " ali ", untrusted: "1" })).toEqual({
      app: "desk",
      status: "",
      text: "ali",
      untrusted: true,
    });
  });

  it("takes the first of a repeated parameter", () => {
    expect(filterFrom({ status: ["paused", "failed"] }).status).toBe("paused");
  });

  it("an empty filter matches everything", () => {
    expect(matches(RUN, filterFrom({}))).toBe(true);
  });

  it.each([
    [{ app: "other" }, false],
    [{ app: "desk" }, true],
    [{ status: "failed" }, true],
    [{ status: "completed" }, false],
    [{ q: "ALICE" }, true],
    [{ q: "01ab" }, true],
    [{ q: "tick" }, true],
    [{ q: "bob" }, false],
  ])("filter %j matches: %s", (params, expected) => {
    expect(matches(RUN, filterFrom(params))).toBe(expected);
  });

  it("can keep to runs that read untrusted content", () => {
    expect(matches({ ...RUN, tainted: false }, filterFrom({ untrusted: "1" }))).toBe(false);
  });
});

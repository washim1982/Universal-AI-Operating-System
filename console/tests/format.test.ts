import { describe, expect, it } from "vitest";

import { json, person, source, usd, when } from "@/lib/format";

describe("how things read", () => {
  it.each([
    ["user:ada@acme", "ada"],
    ["service:ops@acme", "ops"],
    ["agent:refund-agent@acme", "agent:refund-agent@acme"],
    [null, "nobody"],
  ])("names %s", (principal, name) => {
    expect(person(principal)).toBe(name);
  });

  it.each([
    ["input", "the request"],
    ["tool:web.page.read", "the tool web.page.read"],
    ["memory:01ITEM", "a stored document (01ITEM)"],
    ["suspected:tool:web.page.read", "what looks like an injection, in the tool web.page.read"],
    ["something:new", "something:new"],
  ])("says where %s came from", (name, words) => {
    expect(source(name)).toBe(words);
  });

  it("shows money to the place costs are kept in, and nothing as nothing", () => {
    expect(usd(0)).toBe("$0");
    expect(usd(0.01464)).toBe("$0.0146");
  });

  it("gives every time in UTC, and says nothing for no time", () => {
    expect(when("2026-11-01T23:30:00-05:00")).toBe("2 Nov 2026, 04:30");
    expect(when(null)).toBe("");
  });

  it("indents structured data, and copes with none", () => {
    expect(json({ a: 1 })).toBe('{\n  "a": 1\n}');
    expect(json(undefined)).toBe("");
  });
});

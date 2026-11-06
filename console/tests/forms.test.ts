import { describe, expect, it } from "vitest";

import config from "../next.config";

describe("forms", () => {
  it("carry their origin, even when sent before the page has loaded", async () => {
    // Before hydration a form posts natively. With no-referrer the browser then sends
    // Origin: null, and Next refuses the post as cross-site: seen in a real browser.
    const [rule] = (await config.headers?.()) ?? [];
    const policy = rule?.headers.find((header) => header.key === "Referrer-Policy")?.value;
    expect(policy).toBe("same-origin");
  });
});

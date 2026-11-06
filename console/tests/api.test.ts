import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  accepts,
  answer,
  ApiError,
  approval,
  compareRuns,
  costs,
  discard,
  inbox,
  installations,
  isForbidden,
  latestEvents,
  restartRun,
  run,
  runs,
  uninstall,
  unless403,
} from "@/lib/api";
import { session } from "@/lib/session";

vi.mock("next/navigation", () => ({
  redirect: vi.fn((to: string) => {
    throw new Error(`redirected to ${to}`);
  }),
}));
vi.mock("@/lib/session", () => ({ session: vi.fn() }));

function answers(status: number, body: unknown = []) {
  const fetcher = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

function sent(fetcher: ReturnType<typeof answers>) {
  const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
  return { url, init, headers: new Headers(init.headers) };
}

beforeEach(() => {
  vi.mocked(session).mockResolvedValue({ token: "the-token" } as never);
});

describe("running a run again", () => {
  it("asks for the mode chosen, with only what changes", async () => {
    const fetcher = answers(202, { run_id: "01NEW" });
    expect(await restartRun("a/b", "01R", "fork", { step: "pay", model: "x/y" })).toEqual({ run_id: "01NEW" });
    const { url, init } = sent(fetcher);
    expect(url).toMatch(/\/v1\/apps\/a%2Fb\/runs\/01R\/fork$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ step: "pay", model: "x/y" });
  });

  it("compares two runs, each in its own part of the address", async () => {
    const fetcher = answers(200, { steps: [] });
    await compareRuns("desk", "01A", "0/B");
    expect(sent(fetcher).url).toMatch(/\/v1\/apps\/desk\/runs\/01A\/compare\/0%2FB$/);
  });
});

describe("calling AIOS", () => {
  it("sends the caller's own credential", async () => {
    const fetcher = answers(200);
    await inbox();
    expect(sent(fetcher).headers.get("Authorization")).toBe("Bearer the-token");
  });

  it("sends none when nobody is signed in", async () => {
    vi.mocked(session).mockResolvedValue({} as never);
    const fetcher = answers(200);
    await inbox();
    expect(sent(fetcher).headers.has("Authorization")).toBe(false);
  });

  it("never serves an answer from a cache", async () => {
    const fetcher = answers(200);
    await inbox();
    expect(sent(fetcher).init.cache).toBe("no-store");
  });

  it("sends someone with no credential to sign in", async () => {
    answers(401, { detail: "sign in" });
    await expect(inbox()).rejects.toThrow("redirected to /login");
  });

  it("keeps the API's own reason for a refusal", async () => {
    answers(403, { detail: "whoever an action is for can't approve it" });
    const failure = await approval("a1").catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(403);
    expect((failure as ApiError).message).toBe("whoever an action is for can't approve it");
  });

  it("copes with a refusal that isn't JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("bad gateway", { status: 502 })));
    await expect(inbox()).rejects.toThrow("the server answered 502");
  });
});

describe("addresses", () => {
  it("escapes everything that came from a URL", async () => {
    const fetcher = answers(200, {});
    await run("../../v1/keys", "a/b?c");
    expect(sent(fetcher).url).toContain("/v1/apps/..%2F..%2Fv1%2Fkeys/runs/a%2Fb%3Fc");
  });

  it("limits a run list to what search covers", async () => {
    const fetcher = answers(200);
    await runs("desk");
    expect(sent(fetcher).url).toMatch(/\/v1\/apps\/desk\/runs\?limit=200$/);
  });

  it("asks for the newest events", async () => {
    const fetcher = answers(200);
    await latestEvents("desk", "tool.>", 20);
    expect(sent(fetcher).url).toMatch(/\/v1\/events\?app=desk&pattern=tool\.%3E&limit=20&newest=true$/);
  });

  it("leaves out what wasn't asked for", async () => {
    const fetcher = answers(200, {});
    await costs();
    expect(sent(fetcher).url).toMatch(/\/v1\/costs$/);
  });

  it("names the data choice when uninstalling", async () => {
    const fetcher = answers(200, {});
    await uninstall("desk", "retain");
    expect(sent(fetcher).url).toMatch(/\/v1\/installations\/desk\?data=retain$/);
    expect(sent(fetcher).init.method).toBe("DELETE");
  });
});

describe("what a role doesn't reach", () => {
  it("is said, not thrown", async () => {
    answers(403, { detail: "role tenant-admin needed" });
    const found = await unless403(() => installations());
    expect(isForbidden(found) && found.forbidden).toBe("role tenant-admin needed");
  });

  it("anything else is still an error", async () => {
    answers(500, { detail: "broken" });
    await expect(unless403(() => installations())).rejects.toThrow("broken");
  });

  it("an empty answer is fine", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
    await expect(discard("01DL")).resolves.toBeUndefined();
  });

  it("a detail that is a list still reads", async () => {
    answers(422, { detail: [{ msg: "field required" }] });
    await expect(installations()).rejects.toThrow("field required");
  });
});

describe("answering an approval", () => {
  it("posts the verdict and the comment", async () => {
    const fetcher = answers(200, {});
    await answer("a1", "deny", "not this one");
    const { url, init } = sent(fetcher);
    expect(url).toMatch(/\/v1\/approvals\/a1\/deny$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ comment: "not this one" });
  });

  it("leaves out a comment that wasn't given", async () => {
    const fetcher = answers(200, {});
    await answer("a1", "approve");
    expect(JSON.parse(String(sent(fetcher).init.body))).toEqual({});
  });
});

describe("checking a key", () => {
  it("says whether AIOS took it", async () => {
    answers(200, {});
    await expect(accepts("good")).resolves.toBe(true);
    answers(401, {});
    await expect(accepts("bad")).resolves.toBe(false);
  });
});

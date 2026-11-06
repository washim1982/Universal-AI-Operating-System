import { beforeEach, describe, expect, it, vi } from "vitest";

import { decide } from "@/app/(console)/approvals/actions";
import { signInWithKey, signOut } from "@/app/login/actions";
import { accepts, answer, ApiError } from "@/lib/api";
import { signOutUrl } from "@/lib/oidc";
import { session } from "@/lib/session";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("next/navigation", () => ({
  redirect: vi.fn((to: string) => {
    throw new Error(`redirected to ${to}`);
  }),
}));
vi.mock("@/lib/session", () => ({ session: vi.fn() }));
vi.mock("@/lib/oidc", () => ({ signOutUrl: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  accepts: vi.fn(),
  answer: vi.fn(),
}));

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [name, value] of Object.entries(fields)) data.set(name, value);
  return data;
}

function fakeSession(fields: object = {}) {
  const current = { save: vi.fn(), destroy: vi.fn(), ...fields };
  vi.mocked(session).mockResolvedValue(current as never);
  return current;
}

beforeEach(() => {
  vi.mocked(answer).mockReset();
  vi.mocked(accepts).mockReset();
});

describe("answering", () => {
  it("passes the verdict and comment on, and goes back to the inbox", async () => {
    await expect(decide("a1", {}, form({ verdict: "approve", comment: " fine " }))).rejects.toThrow(
      "redirected to /approvals",
    );
    expect(answer).toHaveBeenCalledWith("a1", "approve", "fine");
  });

  it("sends no comment for a blank one", async () => {
    await expect(decide("a1", {}, form({ verdict: "deny", comment: "  " }))).rejects.toThrow();
    expect(answer).toHaveBeenCalledWith("a1", "deny", undefined);
  });

  it("won't answer without a clear verdict", async () => {
    await expect(decide("a1", {}, form({ verdict: "yes" }))).resolves.toEqual({
      error: "Choose approve or deny.",
    });
    expect(answer).not.toHaveBeenCalled();
  });

  it.each([
    [403, "whoever an action is for can't approve it"],
    [409, "approval a1 is already granted"],
  ])("shows why AIOS said no with a %i", async (status, reason) => {
    vi.mocked(answer).mockRejectedValue(new ApiError(status, reason));
    await expect(decide("a1", {}, form({ verdict: "approve" }))).resolves.toEqual({
      error: reason,
    });
  });

  it("doesn't hide a failure it doesn't understand", async () => {
    vi.mocked(answer).mockRejectedValue(new TypeError("fetch failed"));
    await expect(decide("a1", {}, form({ verdict: "approve" }))).rejects.toThrow("fetch failed");
  });
});

describe("signing in with a key", () => {
  it("keeps a key AIOS accepts", async () => {
    const current = fakeSession();
    vi.mocked(accepts).mockResolvedValue(true);
    await expect(signInWithKey({}, form({ key: " aios_key " }))).rejects.toThrow(
      "redirected to /approvals",
    );
    expect(accepts).toHaveBeenCalledWith("aios_key");
    expect(current).toMatchObject({ token: "aios_key" });
    expect(current.save).toHaveBeenCalled();
  });

  it("refuses one it doesn't, and keeps nothing", async () => {
    const current = fakeSession();
    vi.mocked(accepts).mockResolvedValue(false);
    await expect(signInWithKey({}, form({ key: "nope" }))).resolves.toEqual({
      error: "That key wasn't accepted.",
    });
    expect(current.save).not.toHaveBeenCalled();
  });

  it("asks for one if it is blank", async () => {
    await expect(signInWithKey({}, form({ key: " " }))).resolves.toEqual({
      error: "Paste an API key.",
    });
    expect(accepts).not.toHaveBeenCalled();
  });
});

describe("signing out", () => {
  it("forgets a key, and stays here", async () => {
    const current = fakeSession({ token: "aios_key" });
    await expect(signOut()).rejects.toThrow("redirected to /login");
    expect(current.destroy).toHaveBeenCalled();
    expect(signOutUrl).not.toHaveBeenCalled();
  });

  it("ends a provider's session as well as ours", async () => {
    vi.stubEnv("OIDC_ISSUER", "http://127.0.0.1:18180/realms/aios");
    fakeSession({ token: "t", refreshToken: "r" });
    vi.mocked(signOutUrl).mockResolvedValue("http://idp/logout");
    await expect(signOut()).rejects.toThrow("redirected to http://idp/logout");
  });
});

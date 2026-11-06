import { NextRequest } from "next/server";
import * as client from "openid-client";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GET as callback } from "@/app/auth/callback/route";
import { sessionSecret } from "@/lib/config";
import { beginSignIn, finishSignIn } from "@/lib/oidc";
import { session, sessionOptions } from "@/lib/session";
import { proxy } from "@/proxy";

const iron = vi.hoisted(() => ({ session: {} as Record<string, unknown> }));

vi.mock("next/headers", () => ({ cookies: vi.fn(async () => ({})) }));
vi.mock("iron-session", () => ({ getIronSession: vi.fn(async () => iron.session) }));
vi.mock("openid-client", () => ({
  discovery: vi.fn(async () => ({ configured: true })),
  None: vi.fn(() => "no secret"),
  ClientSecretPost: vi.fn(() => "a secret"),
  allowInsecureRequests: "insecure",
  randomPKCECodeVerifier: vi.fn(() => "the-verifier"),
  randomState: vi.fn(() => "the-state"),
  calculatePKCECodeChallenge: vi.fn(async () => "the-challenge"),
  buildAuthorizationUrl: vi.fn(() => new URL("http://idp/authorize")),
  authorizationCodeGrant: vi.fn(async () => ({
    access_token: "t",
    refresh_token: "r",
    expires_in: 60,
    claims: () => ({ preferred_username: "lee" }),
  })),
  refreshTokenGrant: vi.fn(async () => ({ access_token: "t2", expires_in: 60 })),
}));

function fakeSession(fields: object = {}) {
  iron.session = { save: vi.fn(), destroy: vi.fn(), ...fields };
  return iron.session as Record<string, unknown> & {
    save: ReturnType<typeof vi.fn>;
    destroy: ReturnType<typeof vi.fn>;
  };
}

beforeEach(() => {
  vi.stubEnv("OIDC_ISSUER", "http://127.0.0.1:18180/realms/aios");
  vi.stubEnv("CONSOLE_SESSION_SECRET", "x".repeat(32));
});

describe("the session cookie", () => {
  it("can't be read by a script or sent from another site", () => {
    const { cookieOptions } = sessionOptions();
    expect(cookieOptions).toMatchObject({ httpOnly: true, sameSite: "lax", path: "/" });
  });

  it("won't run in production without a secret", () => {
    vi.stubEnv("CONSOLE_SESSION_SECRET", "");
    vi.stubEnv("NODE_ENV", "production");
    expect(() => sessionSecret()).toThrow("set CONSOLE_SESSION_SECRET");
  });

  it("won't take a short one", () => {
    vi.stubEnv("CONSOLE_SESSION_SECRET", "short");
    expect(() => sessionSecret()).toThrow("at least 32 characters");
  });

  it("makes one for development, and keeps it", () => {
    vi.stubEnv("CONSOLE_SESSION_SECRET", "");
    vi.stubEnv("NODE_ENV", "development");
    expect(sessionSecret()).toHaveLength(64);
    expect(sessionSecret()).toBe(sessionSecret());
  });

  it("reads the session from the request's cookies", async () => {
    fakeSession({ token: "t" });
    await expect(session()).resolves.toMatchObject({ token: "t" });
  });
});

describe("signing in", () => {
  it("uses PKCE and a state, so a stolen code is no use", async () => {
    const { verifier, state } = await beginSignIn();
    expect(verifier).toBe("the-verifier");
    expect(state).toBe("the-state");
    expect(client.buildAuthorizationUrl).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        code_challenge: "the-challenge",
        code_challenge_method: "S256",
        state: "the-state",
      }),
    );
  });

  it("checks the answer against its own address, not one a request claims", async () => {
    await finishSignIn(new URL("http://evil.example/auth/callback?code=c&state=s"), "v", "s");
    const [, url, checks] = vi.mocked(client.authorizationCodeGrant).mock.calls.at(-1)!;
    expect(String(url)).toBe("http://127.0.0.1:3000/auth/callback?code=c&state=s");
    expect(checks).toEqual({ pkceCodeVerifier: "v", expectedState: "s" });
  });
});

describe("coming back from the provider", () => {
  const request = () => new NextRequest("http://127.0.0.1:3000/auth/callback?code=c&state=s");

  it("keeps the tokens and clears what it no longer needs", async () => {
    const current = fakeSession({ verifier: "v", state: "s" });
    const response = await callback(request());
    expect(response.headers.get("location")).toBe("http://127.0.0.1:3000/approvals");
    expect(current).toMatchObject({ token: "t", refreshToken: "r", name: "lee" });
    expect(current.verifier).toBeUndefined();
    expect(current.state).toBeUndefined();
  });

  it("turns away an answer it wasn't waiting for", async () => {
    const current = fakeSession();
    const response = await callback(request());
    expect(response.headers.get("location")).toBe("http://127.0.0.1:3000/login?failed=1");
    expect(current.token).toBeUndefined();
  });

  it("turns away an answer the provider's library rejects", async () => {
    const current = fakeSession({ verifier: "v", state: "s" });
    vi.mocked(client.authorizationCodeGrant).mockRejectedValueOnce(new Error("state mismatch"));
    const response = await callback(request());
    expect(response.headers.get("location")).toBe("http://127.0.0.1:3000/login?failed=1");
    expect(current.token).toBeUndefined();
  });
});

describe("staying signed in", () => {
  const request = () => new NextRequest("http://127.0.0.1:3000/approvals");

  it("renews a token that is about to run out", async () => {
    const current = fakeSession({
      token: "old",
      refreshToken: "r",
      expiresAt: Date.now() + 5_000,
    });
    await proxy(request());
    expect(current).toMatchObject({ token: "t2", refreshToken: "r" });
    expect(current.save).toHaveBeenCalled();
  });

  it("leaves a fresh one alone", async () => {
    const current = fakeSession({
      token: "old",
      refreshToken: "r",
      expiresAt: Date.now() + 120_000,
    });
    await proxy(request());
    expect(current.token).toBe("old");
    expect(current.save).not.toHaveBeenCalled();
  });

  it("leaves an API key alone", async () => {
    const current = fakeSession({ token: "aios_key" });
    await proxy(request());
    expect(current.save).not.toHaveBeenCalled();
  });

  it("ends the session when the provider won't renew it", async () => {
    const current = fakeSession({ token: "old", refreshToken: "r", expiresAt: Date.now() });
    vi.mocked(client.refreshTokenGrant).mockRejectedValueOnce(new Error("revoked"));
    await proxy(request());
    expect(current.destroy).toHaveBeenCalled();
  });
});

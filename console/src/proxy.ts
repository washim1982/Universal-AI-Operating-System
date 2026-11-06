/** Keeps a sign-in alive. A page can't set a cookie while it renders, so renewal happens here. */

import { getIronSession } from "iron-session";
import { NextResponse, type NextRequest } from "next/server";

import { renew } from "./lib/oidc";
import { sessionOptions, type Session } from "./lib/session";

const MARGIN = 30_000;

export async function proxy(request: NextRequest) {
  const response = NextResponse.next();
  const session = await getIronSession<Session>(request, response, sessionOptions());
  const { refreshToken, expiresAt } = session;
  if (refreshToken && expiresAt && expiresAt - Date.now() < MARGIN) {
    try {
      const tokens = await renew(refreshToken);
      session.token = tokens.token;
      session.refreshToken = tokens.refreshToken ?? refreshToken;
      session.expiresAt = tokens.expiresAt;
      await session.save();
    } catch {
      session.destroy(); // the provider said no: sign in again
    }
  }
  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

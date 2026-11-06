/** Who is signed in, in a sealed cookie that scripts on the page can't read. */

import { getIronSession, type SessionOptions } from "iron-session";
import { cookies } from "next/headers";

import { publicUrl, sessionSecret } from "./config";

export interface Session {
  /** A bearer token from sign-in, or an API key. */
  token?: string;
  refreshToken?: string;
  /** When ``token`` runs out, in milliseconds since the epoch. */
  expiresAt?: number;
  /** What to call them, if sign-in said. */
  name?: string;
  /** Held only between going to the identity provider and coming back. */
  verifier?: string;
  state?: string;
}

const EIGHT_HOURS = 8 * 60 * 60;

export function sessionOptions(): SessionOptions {
  return {
    cookieName: "aios_console",
    password: sessionSecret(),
    ttl: EIGHT_HOURS,
    cookieOptions: {
      httpOnly: true,
      sameSite: "lax",
      secure: publicUrl.startsWith("https:"),
      path: "/",
    },
  };
}

export async function session() {
  return getIronSession<Session>(await cookies(), sessionOptions());
}

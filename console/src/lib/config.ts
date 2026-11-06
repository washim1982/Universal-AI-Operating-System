/** Where things are. Everything here is read on the server; none of it reaches a browser. */

import { randomBytes } from "node:crypto";

export const server = (process.env.AIOS_SERVER ?? "http://127.0.0.1:8080").replace(/\/$/, "");

/** The console's own address. Redirects are built from this, never from a request's Host header. */
export const publicUrl = (process.env.CONSOLE_URL ?? "http://127.0.0.1:3000").replace(/\/$/, "");

export interface OidcSettings {
  issuer: string;
  clientId: string;
  clientSecret?: string;
}

/** Sign-in through an identity provider, or ``null`` to sign in with an API key. */
export function oidc(): OidcSettings | null {
  const issuer = process.env.OIDC_ISSUER;
  if (!issuer) return null;
  return {
    issuer,
    clientId: process.env.OIDC_CLIENT_ID ?? "aios-console",
    clientSecret: process.env.OIDC_CLIENT_SECRET,
  };
}

const MINIMUM_SECRET = 32;
let generated: string | undefined;

/** The key that seals the session cookie. In development a new one is made for each process. */
export function sessionSecret(): string {
  const secret = process.env.CONSOLE_SESSION_SECRET;
  if (secret) {
    if (secret.length < MINIMUM_SECRET) {
      throw new Error(`CONSOLE_SESSION_SECRET needs at least ${MINIMUM_SECRET} characters`);
    }
    return secret;
  }
  if (process.env.NODE_ENV === "production") {
    throw new Error("set CONSOLE_SESSION_SECRET: sessions can't be sealed without it");
  }
  generated ??= randomBytes(MINIMUM_SECRET).toString("hex");
  return generated;
}

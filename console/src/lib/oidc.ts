/** Sign-in with an identity provider: the authorization code flow with PKCE (ADR-011). */

import * as client from "openid-client";

import { oidc, publicUrl, type OidcSettings } from "./config";

const CALLBACK = `${publicUrl}/auth/callback`;
const SCOPE = "openid profile";

export interface Tokens {
  token: string;
  refreshToken?: string;
  expiresAt?: number;
  name?: string;
}

let discovered: Promise<client.Configuration> | undefined;

function settings(): OidcSettings {
  const found = oidc();
  if (found === null) throw new Error("no identity provider is configured");
  return found;
}

function configuration(): Promise<client.Configuration> {
  if (discovered === undefined) {
    const { issuer, clientId, clientSecret } = settings();
    const attempt = client.discovery(
      new URL(issuer),
      clientId,
      undefined,
      clientSecret ? client.ClientSecretPost(clientSecret) : client.None(),
      // A local Keycloak has no TLS. Anywhere else, the library's refusal stands.
      new URL(issuer).hostname === "127.0.0.1"
        ? { execute: [client.allowInsecureRequests] }
        : undefined,
    );
    // A provider that was down shouldn't stay down for this process.
    attempt.catch(() => {
      discovered = undefined;
    });
    discovered = attempt;
  }
  return discovered;
}

function tokensFrom(response: client.TokenEndpointResponse): Tokens {
  const lifetime = response.expires_in;
  return {
    token: response.access_token,
    refreshToken: response.refresh_token,
    expiresAt: lifetime === undefined ? undefined : Date.now() + lifetime * 1000,
  };
}

/** A name to show. An identity provider's ``sub`` is an id, which means nothing to a person. */
function nameIn(response: client.TokenEndpointResponse & client.TokenEndpointResponseHelpers) {
  const claims = response.claims();
  const name = claims?.preferred_username ?? claims?.name;
  return typeof name === "string" ? name : undefined;
}

export async function beginSignIn(): Promise<{ url: URL; verifier: string; state: string }> {
  const config = await configuration();
  const verifier = client.randomPKCECodeVerifier();
  const state = client.randomState();
  const url = client.buildAuthorizationUrl(config, {
    redirect_uri: CALLBACK,
    scope: SCOPE,
    state,
    code_challenge: await client.calculatePKCECodeChallenge(verifier),
    code_challenge_method: "S256",
  });
  return { url, verifier, state };
}

export async function finishSignIn(
  callback: URL,
  verifier: string,
  state: string,
): Promise<Tokens> {
  const config = await configuration();
  // The provider sent the browser to CALLBACK, so that is the address to check against.
  const url = new URL(`${CALLBACK}${callback.search}`);
  const response = await client.authorizationCodeGrant(config, url, {
    pkceCodeVerifier: verifier,
    expectedState: state,
  });
  return { ...tokensFrom(response), name: nameIn(response) };
}

export async function renew(refreshToken: string): Promise<Tokens> {
  return tokensFrom(await client.refreshTokenGrant(await configuration(), refreshToken));
}

/** Where to send a browser to end its session at the provider too, if the provider can. */
export async function signOutUrl(): Promise<string | null> {
  const config = await configuration();
  if (config.serverMetadata().end_session_endpoint === undefined) return null;
  return client
    .buildEndSessionUrl(config, {
      client_id: settings().clientId,
      post_logout_redirect_uri: `${publicUrl}/login`,
    })
    .toString();
}

import { NextResponse } from "next/server";

import { beginSignIn } from "@/lib/oidc";
import { session } from "@/lib/session";

export async function GET() {
  const { url, verifier, state } = await beginSignIn();
  const current = await session();
  current.verifier = verifier;
  current.state = state;
  await current.save();
  return NextResponse.redirect(url);
}

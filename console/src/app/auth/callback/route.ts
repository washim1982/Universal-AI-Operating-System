import { NextResponse, type NextRequest } from "next/server";

import { publicUrl } from "@/lib/config";
import { finishSignIn } from "@/lib/oidc";
import { session } from "@/lib/session";

export async function GET(request: NextRequest) {
  const current = await session();
  const { verifier, state } = current;
  current.verifier = undefined;
  current.state = undefined;
  if (!verifier || !state) {
    await current.save();
    return NextResponse.redirect(`${publicUrl}/login?failed=1`);
  }
  try {
    const tokens = await finishSignIn(request.nextUrl, verifier, state);
    current.token = tokens.token;
    current.refreshToken = tokens.refreshToken;
    current.expiresAt = tokens.expiresAt;
    current.name = tokens.name;
  } catch {
    await current.save();
    return NextResponse.redirect(`${publicUrl}/login?failed=1`);
  }
  await current.save();
  return NextResponse.redirect(`${publicUrl}/approvals`);
}

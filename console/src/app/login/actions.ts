"use server";

import { redirect } from "next/navigation";

import { accepts } from "@/lib/api";
import { oidc } from "@/lib/config";
import { signOutUrl } from "@/lib/oidc";
import { session } from "@/lib/session";

export interface SignInState {
  error?: string;
}

export async function signInWithKey(_: SignInState, form: FormData): Promise<SignInState> {
  const key = String(form.get("key") ?? "").trim();
  if (!key) return { error: "Paste an API key." };
  if (!(await accepts(key))) return { error: "That key wasn't accepted." };
  const current = await session();
  current.token = key;
  await current.save();
  redirect("/approvals");
}

export async function signOut(): Promise<void> {
  const current = await session();
  const signedIn = current.refreshToken !== undefined;
  current.destroy();
  const elsewhere = signedIn && oidc() !== null ? await signOutUrl() : null;
  redirect(elsewhere ?? "/login");
}

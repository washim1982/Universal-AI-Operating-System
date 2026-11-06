import type { Metadata } from "next";

import { KeyForm } from "@/components/KeyForm";
import { oidc } from "@/lib/config";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ failed?: string }>;
}) {
  const { failed } = await searchParams;
  return (
    <main className="signin">
      <h1>AIOS console</h1>
      {failed && (
        <p className="notice danger" role="alert">
          Signing in didn&apos;t work. Try again.
        </p>
      )}
      {oidc() ? (
        <a className="button primary" href="/auth/login">
          Sign in
        </a>
      ) : (
        <KeyForm />
      )}
    </main>
  );
}

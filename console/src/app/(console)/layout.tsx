import Link from "next/link";
import type { ReactNode } from "react";

import { Refresher } from "@/components/Refresher";
import { inbox, me } from "@/lib/api";
import { person } from "@/lib/format";
import { session } from "@/lib/session";

import { signOut } from "../login/actions";

export const dynamic = "force-dynamic";

export default async function ConsoleLayout({ children }: { children: ReactNode }) {
  const [caller, waiting, { name }] = await Promise.all([me(), inbox(), session()]);
  return (
    <>
      <header className="top">
        <strong className="brand">AIOS</strong>
        <nav aria-label="Main">
          <Link href="/approvals">
            Approvals
            {waiting.length > 0 && <span className="count">{waiting.length}</span>}
          </Link>
          <Link href="/runs">Runs</Link>
          <Link href="/apps">Apps</Link>
          <Link href="/agents">Agents</Link>
          <Link href="/costs">Costs</Link>
          <Link href="/policies">Policies</Link>
          <Link href="/memory">Memory</Link>
          <Link href="/events">Events</Link>
        </nav>
        <div className="who">
          <span title={caller.principal}>
            {name ?? person(caller.principal)} · {caller.tenant}
          </span>
          <form action={signOut}>
            <button className="link" type="submit">
              Sign out
            </button>
          </form>
        </div>
      </header>
      <main className="page">{children}</main>
      <Refresher />
    </>
  );
}

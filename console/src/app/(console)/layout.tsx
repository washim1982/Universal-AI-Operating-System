import Link from "next/link";
import type { ReactNode } from "react";

import { Refresher } from "@/components/Refresher";
import { ApiError, inbox, me, tasks } from "@/lib/api";
import { person } from "@/lib/format";
import { session } from "@/lib/session";

import { signOut } from "../login/actions";

export const dynamic = "force-dynamic";

export default async function ConsoleLayout({ children }: { children: ReactNode }) {
  const [caller, waiting, work, { name }] = await Promise.all([
    me(),
    inbox(),
    tasks().catch((error: unknown) => {
      // someone who can't do tasks, or an AIOS from before them: no count, still a console
      if (error instanceof ApiError && (error.status === 403 || error.status === 404)) return [];
      throw error;
    }),
    session(),
  ]);
  const todo = work.length;
  return (
    <>
      <header className="top">
        <strong className="brand">AIOS</strong>
        <nav aria-label="Main">
          <Link href="/approvals">
            Approvals
            {waiting.length > 0 && <span className="count">{waiting.length}</span>}
          </Link>
          <Link href="/tasks">
            Tasks
            {todo > 0 && <span className="count">{todo}</span>}
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
          <Link href="/profile" title={caller.principal}>
            {name ?? person(caller.principal)} · {caller.tenant}
          </Link>
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

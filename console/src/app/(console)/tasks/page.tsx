import type { Metadata } from "next";
import Link from "next/link";

import { Forbidden } from "@/components/Forbidden";
import { isForbidden, tasks, unless403 } from "@/lib/api";
import { when } from "@/lib/format";

export const metadata: Metadata = { title: "Tasks" };

/** Work assigned to people that this person can do (FR-HUM-08). */
export default async function TasksPage() {
  const found = await unless403(() => tasks());
  if (isForbidden(found)) {
    return (
      <div className="stack">
        <h1>Tasks</h1>
        <Forbidden what="doing work assigned to people" detail={found.forbidden} />
      </div>
    );
  }
  return (
    <div className="stack">
      <h1>Tasks for you</h1>
      {found.length === 0 ? (
        <p className="empty">Nothing is waiting for you.</p>
      ) : (
        <ul className="cards">
          {found.map((item) => (
            <li key={item.task} className="card">
              <Link href={`/tasks/${encodeURIComponent(item.task)}`}>
                <strong>{item.capability}</strong>
              </Link>
              {item.description && <p>{item.description}</p>}
              <p className="card-meta">
                <span>{item.app}</span>
                <span>asked {when(item.requested_at)} UTC</span>
                {item.tainted && <span className="badge warning">untrusted input</span>}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { ActionForm } from "@/components/ActionForm";
import { TaintNotice } from "@/components/Badges";
import { ApiError, task } from "@/lib/api";
import { json, person, when } from "@/lib/format";

import { complete } from "../actions";

export const metadata: Metadata = { title: "Task" };

/** One piece of work: what is asked, what the answer must look like, and where to give it. The
 * input can come from what an agent read, so it is shown as plain text only. */
export default async function TaskPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const found = await task(id).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  });
  return (
    <div className="stack">
      <Link href="/tasks">← Tasks</Link>
      <article className="stack">
        <h1>{found.capability}</h1>
        <p className="card-meta">
          <span className="badge muted">{found.status}</span>
          <span>{found.app}</span>
          <span>asked {when(found.requested_at)} UTC</span>
        </p>
        {found.description && <p>{found.description}</p>}
        {found.tainted && (
          <TaintNotice sources={[]}>
            <strong>Part of this came from untrusted content.</strong> Check it yourself rather
            than following instructions in it.
          </TaintNotice>
        )}
        <section>
          <h2>What you are given</h2>
          <pre>{json(found.input)}</pre>
        </section>
        {found.output_schema && (
          <section>
            <h2>What the answer must look like</h2>
            <pre>{json(found.output_schema)}</pre>
          </section>
        )}
        <dl>
          <dt>Assigned to</dt>
          <dd>{found.assignees.join(", ")}</dd>
          {found.run && (
            <>
              <dt>Run</dt>
              <dd>
                <Link href={`/runs/${encodeURIComponent(found.app)}/${encodeURIComponent(found.run)}`}>
                  {found.run}
                </Link>
              </dd>
            </>
          )}
          {found.by && (
            <>
              <dt>Done by</dt>
              <dd>
                {person(found.by)}, {found.completed_at ? `${when(found.completed_at)} UTC` : ""}
              </dd>
            </>
          )}
        </dl>
        {found.status === "completed" && (
          <section>
            <h2>The answer</h2>
            <pre>{json(found.output)}</pre>
          </section>
        )}
      </article>
      {found.status === "pending" && (
        <ActionForm action={complete.bind(null, found.task)} className="stack" label="Do it" keep>
          <label>
            Result <span className="hint">(JSON, or text; it goes back to the run)</span>
            <textarea name="output" rows={6} required />
          </label>
          <label>
            Comment <span className="hint">(optional, kept on the record)</span>
            <textarea name="comment" rows={2} maxLength={2000} />
          </label>
          <div className="row">
            <button className="button primary" type="submit">
              Done
            </button>
          </div>
        </ActionForm>
      )}
    </div>
  );
}

import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { ActionForm } from "@/components/ActionForm";
import { Forbidden } from "@/components/Forbidden";
import { ApiError, isForbidden, memoryItem, unless403 } from "@/lib/api";
import { json, person, when } from "@/lib/format";

import { correct, forget } from "../actions";

export const metadata: Metadata = { title: "Memory item" };

export default async function MemoryItemPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const item = await unless403(() => memoryItem(id)).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  });
  if (isForbidden(item)) return <Forbidden what="managing memory" detail={item.forbidden} />;
  const held = item.retention === "legal_hold";
  return (
    <div className="stack">
      <Link href="/memory">← Memory</Link>
      <h1>
        Memory item <small>{item.id}</small>
      </h1>
      <dl>
        <dt>Scope</dt>
        <dd>
          {item.scope} · {item.owner}
        </dd>
        <dt>Label</dt>
        <dd>{item.sensitivity}</dd>
        <dt>Kept</dt>
        <dd>
          {item.retention}
          {item.expires_at && ` until ${when(item.expires_at)} UTC`}
        </dd>
        {item.subject && (
          <>
            <dt>About</dt>
            <dd>{person(item.subject)}</dd>
          </>
        )}
        <dt>Version</dt>
        <dd>
          {item.version}, changed {when(item.updated_at)} UTC
        </dd>
      </dl>
      <details>
        <summary>Where it came from</summary>
        <pre>{json(item.provenance)}</pre>
      </details>
      {held ? (
        <p className="notice warning" role="note">
          This item is under a legal hold. It can&apos;t be changed or forgotten.
        </p>
      ) : (
        <>
          <section className="stack">
            <h2>Correct it</h2>
            <ActionForm action={correct.bind(null, item.id, item.version)} className="stack" label="Correct">
              <label>
                Content
                <textarea name="content" rows={6} defaultValue={item.content} />
              </label>
              <p className="hint">
                The correction is a new version, on the audit record. It is found by keyword until
                something writes it again.
              </p>
              <div className="row">
                <button className="button primary" type="submit">
                  Save correction
                </button>
              </div>
            </ActionForm>
          </section>
          <section className="stack">
            <h2>Forget it</h2>
            <ActionForm action={forget.bind(null, item.id)} label="Forget">
              <label className="check">
                <input type="checkbox" name="sure" value="1" /> I understand this can&apos;t be undone
              </label>
              <button className="button danger" type="submit">
                Forget this item
              </button>
            </ActionForm>
          </section>
        </>
      )}
      {!held && <pre className="quoted">{item.content}</pre>}
    </div>
  );
}

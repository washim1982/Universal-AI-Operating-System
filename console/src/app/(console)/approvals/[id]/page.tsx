import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { ApprovalDetails } from "@/components/ApprovalDetails";
import { DecisionForm } from "@/components/DecisionForm";
import { ApiError, approval, me } from "@/lib/api";

import { decide } from "../actions";

export const metadata: Metadata = { title: "Approval" };

export default async function ApprovalPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const [found, caller] = await Promise.all([
    approval(id).catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 404) notFound();
      throw error;
    }),
    me(),
  ]);
  const own = found.initiator === caller.principal;
  return (
    <div className="stack">
      <Link href="/approvals">← Approvals</Link>
      <ApprovalDetails approval={found} />
      {found.status !== "pending" ? null : own ? (
        <p className="notice">You started this run, so someone else has to answer.</p>
      ) : (
        <DecisionForm action={decide.bind(null, found.approval)} />
      )}
    </div>
  );
}

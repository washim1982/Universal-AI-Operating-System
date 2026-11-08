import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { ApprovalDetails } from "@/components/ApprovalDetails";
import { DecisionForm } from "@/components/DecisionForm";
import { ApiError, approval, me } from "@/lib/api";

import { decide } from "../actions";

export const metadata: Metadata = { title: "Approval" };

function changeable(payload: unknown, quorum: number): Record<string, unknown> | null {
  if (quorum > 1) return null; // with a quorum, everyone must agree to the same thing
  const record = typeof payload === "object" && payload !== null && !Array.isArray(payload);
  return record ? (payload as Record<string, unknown>) : null;
}

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
  const voted = (found.votes ?? []).some((vote) => vote.by === caller.principal);
  return (
    <div className="stack">
      <Link href="/approvals">← Approvals</Link>
      <ApprovalDetails approval={found} />
      {found.status !== "pending" ? null : own ? (
        <p className="notice">You started this run, so someone else has to answer.</p>
      ) : voted ? (
        <p className="notice">You have said yes. It waits for the others.</p>
      ) : (
        <DecisionForm
          action={decide.bind(null, found.approval)}
          changeable={changeable(found.payload, found.quorum ?? 1)}
        />
      )}
    </div>
  );
}

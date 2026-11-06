import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { ComparisonView } from "@/components/ComparisonView";
import { ApiError, compareRuns } from "@/lib/api";

export const metadata: Metadata = { title: "Compare runs" };

export default async function ComparePage({
  params,
}: {
  params: Promise<{ app: string; id: string; other: string }>;
}) {
  const { app, id, other } = await params;
  const comparison = await compareRuns(app, id, other).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  });
  return (
    <div className="stack">
      <Link href={`/runs/${encodeURIComponent(app)}/${encodeURIComponent(other)}`}>← Run</Link>
      <ComparisonView app={app} comparison={comparison} />
    </div>
  );
}

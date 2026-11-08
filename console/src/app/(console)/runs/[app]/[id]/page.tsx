import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { FeedbackForm } from "@/components/FeedbackForm";
import { Refresher } from "@/components/Refresher";
import { RestartForm } from "@/components/RestartForm";
import { RunControls } from "@/components/RunControls";
import { RunDetailsView } from "@/components/RunDetailsView";
import { TraceExplorer } from "@/components/TraceExplorer";
import { ApiError, run, runEvents, trace } from "@/lib/api";

export const metadata: Metadata = { title: "Run" };

const LIVE = 1_500;

export default async function RunPage({
  params,
}: {
  params: Promise<{ app: string; id: string }>;
}) {
  const { app, id } = await params;
  const [details, events, drawn] = await Promise.all([
    run(app, id),
    runEvents(app, id),
    trace(app, id),
  ]).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  });
  const live = details.status === "running" || details.status === "paused";
  return (
    <div className="stack">
      <Link href="/runs">← Runs</Link>
      <RunDetailsView run={details} events={events} timeline={false} />
      <RunControls app={app} id={id} status={details.status} waiting={details.waiting} />
      <RestartForm
        app={app}
        id={id}
        status={details.status}
        steps={drawn.nodes.map((node) => node.id)}
        failed={drawn.nodes.find((node) => node.status === "failed")?.id ?? null}
      />
      <TraceExplorer trace={drawn} />
      <FeedbackForm app={app} id={id} steps={drawn.nodes.map((node) => node.id)} />
      {live && <Refresher every={LIVE} />}
    </div>
  );
}

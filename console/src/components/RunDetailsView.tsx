import Link from "next/link";

import { json, usd, when } from "@/lib/format";
import type { RestartMode, RunDetails, RunEvent, RunSource } from "@/lib/types";

import { StatusBadge, TaintNotice } from "./Badges";

const MADE: Record<RestartMode, string> = {
  replay: "A replay of",
  fork: "A fork of",
  retry: "A retry of",
};

const MEANT: Record<RestartMode, string> = {
  replay: "Every model and tool call was answered from that run's record.",
  fork: "Side effects were held back.",
  retry: "Side effects happened again, for real.",
};

/** Where a replay, fork or retry came from, and the way to compare the two. */
function Source({ app, run, source }: { app: string; run: string; source: RunSource }) {
  const at = (id: string) => `/runs/${encodeURIComponent(app)}/${encodeURIComponent(id)}`;
  return (
    <p className="notice">
      {MADE[source.mode]}{" "}
      <Link href={at(source.run)}>
        <code>{source.run}</code>
      </Link>
      {source.step && (
        <>
          {" "}
          from step <code>{source.step}</code>
        </>
      )}
      {source.model && (
        <>
          {" "}
          with <code>{source.model}</code>
        </>
      )}
      . {MEANT[source.mode]}{" "}
      <Link href={`${at(source.run)}/compare/${encodeURIComponent(run)}`}>Compare the two</Link>
    </p>
  );
}

export function RunDetailsView({
  run,
  events,
  timeline = true,
}: {
  run: RunDetails;
  events: RunEvent[];
  /** The Trace Explorer has its own, with times from the start. */
  timeline?: boolean;
}) {
  const steps = Object.entries(run.steps);
  return (
    <article className="stack">
      <h1>
        {run.app} <small>{run.run_id}</small>
      </h1>
      <p className="card-meta">
        <StatusBadge status={run.status} />
        {run.usage?.cost_usd !== undefined && <span>{usd(run.usage.cost_usd)}</span>}
      </p>
      {run.source && <Source app={run.app} run={run.run_id} source={run.source} />}
      {run.tainted && (
        <TaintNotice sources={run.taint}>
          <strong>This run read untrusted content.</strong> Whatever it did afterwards may have been
          steered by it, so anything risky needed a person.
        </TaintNotice>
      )}
      {run.error && (
        <p className="notice danger" role="alert">
          {run.error.code}: {run.error.message}
        </p>
      )}
      {run.waiting.length > 0 && <p className="notice">Waiting for {run.waiting.join(", ")}.</p>}
      {steps.length > 0 && (
        <section>
          <h2>Steps</h2>
          <ul className="plain">
            {steps.map(([step, status]) => (
              <li key={step}>
                <code>{step}</code> {status}
              </li>
            ))}
          </ul>
        </section>
      )}
      {run.output !== null && run.output !== undefined && (
        <section>
          <h2>Output</h2>
          <pre>{json(run.output)}</pre>
        </section>
      )}
      {timeline && (
      <section>
        <h2>What happened</h2>
        <ol className="plain timeline">
          {events.map((event) => (
            <li key={event.id}>
              <time dateTime={event.time}>{when(event.time)}</time> <code>{event.type}</code>
            </li>
          ))}
        </ol>
      </section>
      )}
    </article>
  );
}

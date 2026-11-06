import type { Metadata } from "next";

import { ActionForm } from "@/components/ActionForm";
import { EventTable } from "@/components/EventTable";
import { Forbidden } from "@/components/Forbidden";
import { apps, deadLetters, isForbidden, latestEvents, unless403 } from "@/lib/api";
import { when } from "@/lib/format";

import { drop, retry } from "./actions";

export const metadata: Metadata = { title: "Events" };

const PATTERN = /^[a-z0-9>*_.-]{1,200}$/;

export default async function EventsPage({
  searchParams,
}: {
  searchParams: Promise<{ app?: string; pattern?: string }>;
}) {
  const params = await searchParams;
  const installed = await apps();
  const app = installed.find((item) => item.app === params.app)?.app;
  const pattern = params.pattern && PATTERN.test(params.pattern) ? params.pattern : ">";
  const [events, letters] = await Promise.all([
    unless403(() => latestEvents(app, pattern)),
    unless403(deadLetters),
  ]);
  if (isForbidden(events)) return <Forbidden what="reading events" detail={events.forbidden} />;
  return (
    <div className="stack">
      <h1>Events</h1>
      <form className="row filters" role="search" aria-label="Filter events">
        <label>
          App
          <select name="app" defaultValue={app ?? ""}>
            <option value="">All, and the platform</option>
            {installed.map((item) => (
              <option key={item.app}>{item.app}</option>
            ))}
          </select>
        </label>
        <label>
          Type <span className="hint">(e.g. tool.call.* or approval.&gt;)</span>
          <input name="pattern" defaultValue={pattern} />
        </label>
        <button className="button" type="submit">
          Show
        </button>
      </form>
      <p className="hint">The latest 200, newest first. The page keeps itself current.</p>
      <EventTable events={events} />
      <section className="stack">
        <h2>Dead letters</h2>
        {isForbidden(letters) ? (
          <Forbidden what="the dead-letter queue" detail={letters.forbidden} />
        ) : letters.length === 0 ? (
          <p className="empty">Nothing failed for good.</p>
        ) : (
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Event</th>
                  <th scope="col">Consumer</th>
                  <th scope="col" className="number">Tries</th>
                  <th scope="col">Why</th>
                  <th scope="col">Since (UTC)</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {letters.map((letter) => (
                  <tr key={letter.id}>
                    <td>
                      <code>{letter.event.type}</code> <small>{letter.event.appid}</small>
                    </td>
                    <td>{letter.group}</td>
                    <td className="number">{letter.attempts}</td>
                    <td>{letter.reason}</td>
                    <td>{when(letter.dead_at)}</td>
                    <td>
                      <ActionForm action={retry.bind(null, letter.id)} label={`Retry ${letter.id}`}>
                        <button className="button" type="submit">
                          Try again
                        </button>
                      </ActionForm>
                      <ActionForm action={drop.bind(null, letter.id)} label={`Drop ${letter.id}`}>
                        <label className="check">
                          <input type="checkbox" name="sure" value="1" /> sure
                        </label>
                        <button className="button danger" type="submit">
                          Drop
                        </button>
                      </ActionForm>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

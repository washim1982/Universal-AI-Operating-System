import Link from "next/link";

import { json, person, when } from "@/lib/format";
import type { StoredEvent } from "@/lib/types";

export function EventTable({ events }: { events: StoredEvent[] }) {
  if (events.length === 0) return <p className="empty">No events match.</p>;
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th scope="col" className="number">#</th>
            <th scope="col">When (UTC)</th>
            <th scope="col">Type</th>
            <th scope="col">App</th>
            <th scope="col">Who</th>
            <th scope="col">Label</th>
            <th scope="col">Data</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <tr key={event.id}>
              <td className="number">{event.position}</td>
              <td>{when(event.time)}</td>
              <td>
                <code>{event.type}</code>
                {event.tainted && <span className="badge warning">untrusted</span>}
              </td>
              <td>
                {event.appid}
                {event.runid && (
                  <>
                    {" "}
                    <Link href={`/runs/${encodeURIComponent(event.appid)}/${encodeURIComponent(event.runid)}`}>
                      run
                    </Link>
                  </>
                )}
              </td>
              <td title={event.principal}>{person(event.principal)}</td>
              <td>{event.dataclass}</td>
              <td>
                {event.data !== undefined && (
                  <details>
                    <summary>show</summary>
                    <pre>{json(event.data)}</pre>
                  </details>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

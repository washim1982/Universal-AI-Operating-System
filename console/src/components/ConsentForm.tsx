import { agree, refuseVersion } from "@/app/(console)/apps/actions";
import { usd } from "@/lib/format";
import type { Grants, Requested } from "@/lib/types";

import { ActionForm } from "./ActionForm";

const KINDS: Array<{ field: keyof Grants; name: string; title: string }> = [
  { field: "tools", name: "tool", title: "Tools" },
  { field: "data_classes", name: "data_class", title: "Data it may handle" },
  { field: "memory_scopes", name: "memory_scope", title: "Memory" },
  { field: "hosts", name: "host", title: "Hosts it may reach" },
];

/** The consent step (FR-APP-03). What was refused before starts unticked: a "no" holds. */
export function ConsentForm({
  app,
  version,
  requested,
  refused,
}: {
  app: string;
  version: string;
  requested: Requested;
  refused: Grants;
}) {
  return (
    <section className="stack consent">
      <h2>
        Version {version} is waiting for your answer
      </h2>
      <p>
        Tick what it may have. Anything left unticked is refused when it runs, whatever the App&apos;s
        own policies say.
      </p>
      <ActionForm action={agree.bind(null, app)} className="stack" label="Consent">
        {KINDS.map(({ field, name, title }) =>
          requested[field].length === 0 ? null : (
            <fieldset key={field}>
              <legend>{title}</legend>
              {requested[field].map((value) => (
                <label key={value} className="check">
                  <input
                    type="checkbox"
                    name={name}
                    value={value}
                    defaultChecked={!refused[field].includes(value)}
                  />
                  <code>{value}</code>
                  {refused[field].includes(value) && <span className="hint">refused before</span>}
                </label>
              ))}
            </fieldset>
          ),
        )}
        {(requested.per_run_usd !== null || requested.monthly_usd !== null) && (
          <p>
            Budget: {requested.per_run_usd !== null && `${usd(requested.per_run_usd)} a run`}
            {requested.per_run_usd !== null && requested.monthly_usd !== null && ", "}
            {requested.monthly_usd !== null && `${usd(requested.monthly_usd)} a month`}
          </p>
        )}
        {requested.starts.length > 0 && (
          <p>
            Runs start from: {requested.starts.join(", ")}.
            {requested.starts.some((s) => s.startsWith("schedule")) && " A schedule starts runs by itself."}
            {requested.starts.some((s) => s.startsWith("webhook")) &&
              " A webhook starts runs for whoever holds its secret, without signing in."}
          </p>
        )}
        <div className="row">
          <button className="button primary" type="submit">
            Agree to what is ticked
          </button>
        </div>
      </ActionForm>
      <ActionForm action={refuseVersion.bind(null, app)} label="Refuse">
        <button className="button danger" type="submit">
          Refuse version {version}
        </button>
      </ActionForm>
    </section>
  );
}

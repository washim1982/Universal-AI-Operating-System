/** Everything one step did. Values arrive as data and are shown as text, never as markup. */

import { count, duration, json, person, usd, when } from "@/lib/format";
import type { Decision, TraceNode } from "@/lib/types";

const EFFECT_TONE: Record<Decision["effect"], string> = {
  allowed: "good",
  denied: "danger",
  escalated: "warning",
};

const APPROVAL_TONE: Record<string, string> = {
  requested: "warning",
  granted: "good",
  denied: "danger",
  expired: "muted",
  cancelled: "muted",
};

function Value({ label, value }: { label: string; value: unknown }) {
  if (value === null || value === undefined) return null;
  const restricted =
    typeof value === "object" && value !== null && "redacted" in value && Object.keys(value).length === 1;
  return (
    <details>
      <summary>{label}</summary>
      {restricted ? (
        <p className="notice">Restricted data isn&apos;t shown in a trace.</p>
      ) : (
        <pre>{json(value)}</pre>
      )}
    </details>
  );
}

export function StepDetails({ step }: { step: TraceNode }) {
  return (
    <article className="stack step-details" aria-label={`Step ${step.id}`}>
      <h3>
        {step.id} <small>{step.capability ?? step.tool ?? step.type}</small>
      </h3>
      <dl>
        <dt>Status</dt>
        <dd>{step.status}</dd>
        {step.attempts > 1 && (
          <>
            <dt>Attempts</dt>
            <dd>{step.attempts}</dd>
          </>
        )}
        {step.started_at && (
          <>
            <dt>Started</dt>
            <dd>{when(step.started_at)} UTC</dd>
          </>
        )}
        {step.duration_ms !== null && (
          <>
            <dt>Took</dt>
            <dd>{duration(step.duration_ms)}</dd>
          </>
        )}
        <dt>Tokens</dt>
        <dd>
          {count(step.input_tokens)} in · {count(step.output_tokens)} out
        </dd>
        <dt>Cost</dt>
        <dd>{usd(step.cost_usd)}</dd>
      </dl>
      {step.copied && (
        <p className="notice">Kept from the run this one comes from: it didn&apos;t run again.</p>
      )}
      {step.tainted && (
        <p className="notice warning" role="note">
          This step worked with untrusted content.
        </p>
      )}
      {step.error && (
        <p className="notice danger" role="alert">
          {step.error}
        </p>
      )}
      {step.approvals.length > 0 && (
        <section>
          <h4>Approvals</h4>
          <ul className="plain">
            {step.approvals.map((item) => (
              <li key={item.approval}>
                <span className={`badge ${APPROVAL_TONE[item.status] ?? "muted"}`}>{item.status}</span>{" "}
                {item.summary}
                {item.by && <> · by {person(item.by)}</>}
                {item.comment && <blockquote>{item.comment}</blockquote>}
              </li>
            ))}
          </ul>
        </section>
      )}
      {step.model_calls.length > 0 && (
        <section>
          <h4>Model calls</h4>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  <th scope="col" className="number">In</th>
                  <th scope="col" className="number">Cached</th>
                  <th scope="col" className="number">Out</th>
                  <th scope="col" className="number">Cost</th>
                  <th scope="col" className="number">Took</th>
                </tr>
              </thead>
              <tbody>
                {step.model_calls.map((call) => (
                  <tr key={`${call.at}-${call.model}`}>
                    <td>
                      {call.provider}/{call.model}
                      {call.replayed && (
                        <span className="badge muted" title="Answered from the record: no model was asked">
                          replayed
                        </span>
                      )}
                    </td>
                    <td className="number">{count(call.input_tokens)}</td>
                    <td className="number">{count(call.cached_tokens)}</td>
                    <td className="number">{count(call.output_tokens)}</td>
                    <td className="number">{usd(call.cost_usd)}</td>
                    <td className="number">{duration(call.latency_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {step.tool_calls.length > 0 && (
        <section>
          <h4>Tool calls</h4>
          <ul className="plain">
            {step.tool_calls.map((call, index) => (
              <li key={`${call.at}-${index}`}>
                <code>{call.tool}</code> {call.status}
                {call.latency_ms !== null && ` · ${duration(call.latency_ms)}`}
                {call.stubbed && (
                  <span className="badge warning" title="A side effect held back: the tool wasn't called">
                    stubbed
                  </span>
                )}
                {call.replayed && (
                  <span className="badge muted" title="Answered from the record">
                    replayed
                  </span>
                )}
                {call.error && <span className="badge danger">{call.error}</span>}
                <Value label="Arguments" value={call.arguments} />
              </li>
            ))}
          </ul>
        </section>
      )}
      {step.decisions.length > 0 && (
        <section>
          <h4>Policy decisions</h4>
          <ul className="plain">
            {step.decisions.map((decision, index) => (
              <li key={`${decision.at}-${index}`}>
                <span className={`badge ${EFFECT_TONE[decision.effect]}`}>{decision.effect}</span>{" "}
                <code>{decision.action}</code> on <code>{decision.resource}</code>
                {decision.reason && <> · {decision.reason}</>}
                {decision.policies.length > 0 && (
                  <span className="hint"> ({decision.policies.join(", ")})</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      {step.memory.length > 0 && (
        <section>
          <h4>Memory read</h4>
          <ul className="plain">
            {step.memory.map((read, index) => (
              <li key={`${read.at}-${index}`}>
                {read.operation} in {read.scopes.join(", ")}: {read.items} item
                {read.items === 1 ? "" : "s"}
              </li>
            ))}
          </ul>
        </section>
      )}
      <Value label="Input" value={step.input} />
      <Value label="Output" value={step.output} />
    </article>
  );
}

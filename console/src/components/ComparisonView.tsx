import Link from "next/link";

import { json, usd } from "@/lib/format";
import type { Comparison, Side } from "@/lib/types";

function SideCell({ side }: { side: Side }) {
  return (
    <td>
      {side.status}
      {side.copied && (
        <span className="badge muted" title="Kept from the source run, not run again">
          copied
        </span>
      )}
      {side.stubbed > 0 && (
        <span className="badge warning" title="Side effects held back">
          {side.stubbed} stubbed
        </span>
      )}
      {side.error && <span className="badge danger">{side.error}</span>}
    </td>
  );
}

function Same({ same }: { same: boolean }) {
  return same ? <span>same</span> : <span className="badge warning">different</span>;
}

/** Two runs side by side, step by step (FR-OBS-04). Values are shown as text, never as markup. */
export function ComparisonView({ app, comparison }: { app: string; comparison: Comparison }) {
  const { a, b, status, output, cost_usd: cost, steps } = comparison;
  const link = (id: string) => (
    <Link href={`/runs/${encodeURIComponent(app)}/${encodeURIComponent(id)}`}>
      <code>{id}</code>
    </Link>
  );
  const differ = steps.filter((step) => !step.same_output);
  return (
    <article className="stack">
      <h1>Compare runs</h1>
      <p className={comparison.same_output ? "notice" : "notice warning"} role="status">
        {comparison.same_output
          ? "Both runs gave the same output."
          : "The runs gave different outputs."}
      </p>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Step</th>
              <th scope="col">A {link(a)}</th>
              <th scope="col">B {link(b)}</th>
              <th scope="col" className="number">
                Cost A
              </th>
              <th scope="col" className="number">
                Cost B
              </th>
              <th scope="col">Output</th>
            </tr>
          </thead>
          <tbody>
            {steps.map((step) => (
              <tr key={step.step}>
                <th scope="row">
                  <code>{step.step}</code>
                </th>
                <SideCell side={step.a} />
                <SideCell side={step.b} />
                <td className="number">{usd(step.a.cost_usd)}</td>
                <td className="number">{usd(step.b.cost_usd)}</td>
                <td>
                  <Same same={step.same_output} />
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row">Run</th>
              <td>{status[0]}</td>
              <td>{status[1]}</td>
              <td className="number">{usd(cost[0])}</td>
              <td className="number">{usd(cost[1])}</td>
              <td>
                <Same same={comparison.same_output} />
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
      {differ.map((step) => (
        <details key={step.step}>
          <summary>
            <code>{step.step}</code> gave different output
          </summary>
          <div className="side-by-side">
            <pre aria-label={`${step.step} in A`}>{json(step.a.output)}</pre>
            <pre aria-label={`${step.step} in B`}>{json(step.b.output)}</pre>
          </div>
        </details>
      ))}
      <section>
        <h2>Output</h2>
        <div className="side-by-side">
          <pre aria-label="Output of A">{json(output[0])}</pre>
          <pre aria-label="Output of B">{json(output[1])}</pre>
        </div>
      </section>
    </article>
  );
}

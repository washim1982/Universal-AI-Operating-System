import { count, person, usd } from "@/lib/format";
import type { Spend } from "@/lib/types";

export function SpendTable({
  title,
  column,
  rows,
  people = false,
}: {
  title: string;
  column: string;
  rows: Spend[];
  people?: boolean;
}) {
  return (
    <section>
      <h2>{title}</h2>
      {rows.length === 0 ? (
        <p className="empty">Nothing spent.</p>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">{column}</th>
                <th scope="col" className="number">Calls</th>
                <th scope="col" className="number">Tokens in</th>
                <th scope="col" className="number">Tokens out</th>
                <th scope="col" className="number">Cost</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.key}>
                  <td title={row.key}>{people ? person(row.key === "(nobody)" ? null : row.key) : row.key}</td>
                  <td className="number">{count(row.calls)}</td>
                  <td className="number">{count(row.input_tokens)}</td>
                  <td className="number">{count(row.output_tokens)}</td>
                  <td className="number">{usd(row.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

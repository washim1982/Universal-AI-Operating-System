import type { Metadata } from "next";
import Link from "next/link";

import { BudgetBar } from "@/components/BudgetBar";
import { Forbidden } from "@/components/Forbidden";
import { SpendTable } from "@/components/SpendTable";
import { costs, isForbidden, unless403 } from "@/lib/api";
import { count, thisMonth, usd, when } from "@/lib/format";

export const metadata: Metadata = { title: "Costs" };

const MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;

export default async function CostsPage({
  searchParams,
}: {
  searchParams: Promise<{ month?: string }>;
}) {
  const asked = (await searchParams).month ?? "";
  const month = MONTH.test(asked) ? asked : thisMonth();
  const report = await unless403(() => costs(month));
  if (isForbidden(report)) return <Forbidden what="seeing costs" detail={report.forbidden} />;
  return (
    <div className="stack">
      <h1>Costs</h1>
      <form className="row filters" aria-label="Month">
        <label>
          Month
          <input type="month" name="month" defaultValue={month} />
        </label>
        <button className="button" type="submit">
          Show
        </button>
      </form>
      <p className="card-meta">
        <span className="big">{usd(report.cost_usd)}</span>
        <span>{count(report.calls)} model calls in {report.month}</span>
      </p>
      <section className="stack">
        <h2>Budgets</h2>
        {report.budgets.length === 0 ? (
          <p className="empty">
            {month === thisMonth()
              ? "No monthly limits are set. A model policy sets them."
              : "Limits are shown for this month only."}
          </p>
        ) : (
          report.budgets.map((budget) => <BudgetBar key={budget.subject} budget={budget} />)
        )}
      </section>
      <section>
        <h2>Alerts</h2>
        {report.alerts.length === 0 ? (
          <p className="empty">No budget was reached.</p>
        ) : (
          <ul className="plain">
            {report.alerts.map((alert, index) => (
              <li key={`${alert.at}-${index}`}>
                <span className={`badge ${alert.type === "budget.exceeded" ? "danger" : "warning"}`}>
                  {alert.type === "budget.exceeded" ? "limit reached" : "soft limit"}
                </span>{" "}
                {alert.app}, {alert.level ?? alert.budget}
                {alert.limit !== null && ` at ${alert.budget === "cost" ? usd(alert.limit) : alert.limit}`}
                {" · "}
                <time dateTime={alert.at}>{when(alert.at)} UTC</time>
                {alert.run && (
                  <>
                    {" · "}
                    <Link href={`/runs/${encodeURIComponent(alert.app)}/${encodeURIComponent(alert.run)}`}>
                      the run
                    </Link>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
      <SpendTable title="By App" column="App" rows={report.by_app} />
      <SpendTable title="By agent" column="Agent" rows={report.by_agent} />
      <SpendTable title="By model" column="Model" rows={report.by_model} />
      <SpendTable title="By person" column="Person" rows={report.by_user} people />
    </div>
  );
}

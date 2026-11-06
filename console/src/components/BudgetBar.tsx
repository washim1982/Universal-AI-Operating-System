import { share, usd } from "@/lib/format";
import type { Budget } from "@/lib/types";

/** How much of a limit is spent, where the soft limit starts, and how near the end it is. */
export function BudgetBar({ budget }: { budget: Budget }) {
  const spent = share(budget.spent_usd, budget.limit_usd);
  const soft = share(budget.soft_usd, budget.limit_usd);
  const tone =
    budget.spent_usd >= budget.limit_usd ? "danger" : budget.spent_usd >= budget.soft_usd ? "warning" : "good";
  return (
    <div className="budget">
      <div className="row spread">
        <span>
          <strong>{budget.level}</strong> <code>{budget.subject}</code>
        </span>
        <span>
          {usd(budget.spent_usd)} of {usd(budget.limit_usd)}
        </span>
      </div>
      <div
        className={`bar ${tone}`}
        role="meter"
        aria-label={`${budget.subject} spent`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={spent}
        aria-valuetext={`${spent}% spent; the soft limit is at ${soft}%`}
      >
        <span className="fill" style={{ width: `${spent}%` }} />
        <span className="soft" style={{ left: `${soft}%` }} title={`soft limit ${usd(budget.soft_usd)}`} />
      </div>
    </div>
  );
}

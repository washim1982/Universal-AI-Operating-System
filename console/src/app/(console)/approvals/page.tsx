import type { Metadata } from "next";

import { ApprovalList } from "@/components/ApprovalList";
import { Forbidden } from "@/components/Forbidden";
import { inbox, isForbidden, unless403 } from "@/lib/api";
import type { ApprovalStatus } from "@/lib/types";

export const metadata: Metadata = { title: "Approvals" };

const STATUSES: ApprovalStatus[] = ["pending", "granted", "denied", "expired", "cancelled"];

export default async function ApprovalsPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string }>;
}) {
  const asked = (await searchParams).status;
  const status = STATUSES.find((value) => value === asked);
  if (!status) {
    return (
      <div className="stack">
        <h1>Waiting for you</h1>
        <History />
        <ApprovalList approvals={await inbox()} />
      </div>
    );
  }
  const found = await unless403(() => inbox(status, true));
  return (
    <div className="stack">
      <h1>Approvals: {status}</h1>
      <History current={status} />
      {isForbidden(found) ? (
        <Forbidden what="seeing everyone's approvals" detail={found.forbidden} />
      ) : (
        <ApprovalList approvals={found} empty={`No approvals are ${status}.`} />
      )}
    </div>
  );
}

function History({ current }: { current?: ApprovalStatus }) {
  return (
    <form className="row filters" aria-label="Approvals to show">
      <label>
        Show
        <select name="status" defaultValue={current ?? ""}>
          <option value="">waiting for me</option>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              everyone&apos;s {value}
            </option>
          ))}
        </select>
      </label>
      <button className="button" type="submit">
        Show
      </button>
    </form>
  );
}

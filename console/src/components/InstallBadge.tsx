import type { InstallStatus } from "@/lib/types";

const TONE: Record<InstallStatus, string> = {
  "awaiting-consent": "warning",
  active: "good",
  uninstalled: "muted",
};

export function InstallBadge({ status }: { status: InstallStatus }) {
  return <span className={`badge ${TONE[status]}`}>{status.replace("-", " ")}</span>;
}

import { resume, stop } from "@/app/(console)/agents/actions";

import { ActionForm } from "./ActionForm";

/** Stop or restart an agent or an App. Only shown to someone who can use the kill switch. */
export function StopForm({
  kind,
  name,
  stopped,
}: {
  kind: "agent" | "app";
  name: string;
  stopped: boolean;
}) {
  if (stopped) {
    return (
      <ActionForm action={resume.bind(null, kind, name)} label={`Restart ${name}`}>
        <button className="button" type="submit">
          Let it work again
        </button>
      </ActionForm>
    );
  }
  return (
    <ActionForm action={stop.bind(null, kind, name)} label={`Stop ${name}`}>
      <input name="reason" placeholder="Why" aria-label={`Why stop ${name}`} maxLength={500} required />
      <button className="button danger" type="submit">
        Stop
      </button>
    </ActionForm>
  );
}

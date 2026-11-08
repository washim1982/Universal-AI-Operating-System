import type { Metadata } from "next";

import { ActionForm } from "@/components/ActionForm";
import { contact, me } from "@/lib/api";

import { saveContact } from "./actions";

export const metadata: Metadata = { title: "You" };

/** Who you are to AIOS, and where it tells you about approvals for you. */
export default async function ProfilePage() {
  const [caller, found] = await Promise.all([me(), contact()]);
  return (
    <div className="stack">
      <h1>You</h1>
      <dl>
        <dt>Principal</dt>
        <dd>
          <code>{caller.principal}</code>
        </dd>
        <dt>Tenant</dt>
        <dd>{caller.tenant}</dd>
        <dt>Roles</dt>
        <dd>{caller.roles.join(", ") || "none"}</dd>
      </dl>
      <section className="stack">
        <h2>Where you are told about approvals</h2>
        <p className="hint">
          When an approval is for you, AIOS sends you a link of your own by Slack, Teams or email,
          if this server has them set up. Whoever has the link can answer as you until it expires,
          so give addresses only you read.
        </p>
        <ActionForm action={saveContact} className="stack" label="Where you are told" keep>
          <label>
            Email
            <input name="email" type="email" defaultValue={found.email ?? ""} maxLength={320} />
          </label>
          <label>
            Slack member ID <span className="hint">(in Slack: your profile, ⋮, Copy member ID)</span>
            <input
              name="slack"
              defaultValue={found.slack ?? ""}
              pattern="[UW][A-Z0-9]{2,20}"
              maxLength={24}
            />
          </label>
          <div className="row">
            <button className="button primary" type="submit">
              Save
            </button>
          </div>
        </ActionForm>
      </section>
    </div>
  );
}

import { feedback } from "@/app/(console)/runs/actions";

import { ActionForm } from "./ActionForm";

/** Rate or correct what a run, or one of its steps, did (FR-HUM-09). A correction of the whole
 * run can become an evaluation case; with "remember", the App's agents can find it too. */
export function FeedbackForm({ app, id, steps }: { app: string; id: string; steps: string[] }) {
  return (
    <section className="stack">
      <h2>Feedback</h2>
      <ActionForm action={feedback.bind(null, app, id)} className="stack" label="Feedback" keep>
        <div className="row">
          <label>
            About
            <select name="step" defaultValue="">
              <option value="">the whole run</option>
              {steps.map((step) => (
                <option key={step} value={step}>
                  step {step}
                </option>
              ))}
            </select>
          </label>
          <label>
            Rating
            <select name="rating" defaultValue="">
              <option value="">none</option>
              <option value="good">good</option>
              <option value="bad">bad</option>
            </select>
          </label>
        </div>
        <label>
          Correction <span className="hint">(what a right result would have been: JSON, or text)</span>
          <textarea name="correction" rows={3} />
        </label>
        <label>
          Comment
          <textarea name="comment" rows={2} maxLength={2000} />
        </label>
        <label className="check">
          <input type="checkbox" name="remember" /> Keep it in the App&apos;s memory, for its agents
        </label>
        <div className="row">
          <button className="button" type="submit">
            Send feedback
          </button>
        </div>
      </ActionForm>
    </section>
  );
}

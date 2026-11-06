/** A page this person's roles don't reach. Not an error: the API said no, and why. */
export function Forbidden({ what, detail }: { what: string; detail: string }) {
  return (
    <div className="notice" role="note">
      <p>
        <strong>Your roles don&apos;t include {what}.</strong> Ask a tenant admin if you need it.
      </p>
      <p className="hint">AIOS said: {detail}</p>
    </div>
  );
}

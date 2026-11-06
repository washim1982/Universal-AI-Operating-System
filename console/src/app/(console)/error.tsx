"use client";

export default function Failed({ reset }: { reset: () => void }) {
  return (
    <div className="stack">
      <h1>That didn&apos;t load</h1>
      <p className="notice danger" role="alert">
        The console couldn&apos;t reach AIOS, or AIOS refused. Your work is safe: nothing was changed.
      </p>
      <button className="button" type="button" onClick={reset}>
        Try again
      </button>
    </div>
  );
}

# AIOS console

The web console (WP 2.6, WP 3.2 and WP 3.3):

- **Approvals**: the inbox, each request in full, approve and deny; everyone's, by status, for an
  auditor.
- **Runs**: search, a live view, pause, resume and cancel, and the **Trace Explorer**: the run's own
  workflow as a graph, each step's model calls, tool calls, policy decisions, memory reads,
  approvals, tokens, cost and time, and a timeline. Restricted data isn't shown.
- **Run again**: replay a finished run from its record, fork it from a step with a new input, step
  input or model and side effects held back, or retry it for real. A run says what it came from and
  links to a step-by-step comparison of the two; copied steps, replayed calls and held-back calls
  are marked in the trace.
- **Apps**: consent (what was refused before starts unticked), configuration, versions and rollback,
  export and uninstall.
- **Agents**: what each may do and has done, and the kill switch.
- **Costs**, **Policies** (with a simulator), **Memory** (search, correct, forget) and **Events**
  (with dead letters).

A page a person's roles don't reach says so, with the API's reason. It isn't hidden by the console's
own idea of roles: the API decides.

It is a Next.js app that calls the AIOS API from its own server, with each person's own credential.
So it has no permissions of its own: what someone can see and do here is what the API lets them see
and do, and every decision about an approval is made by AIOS, not by this code.

## Running it

Start AIOS, then, with Node 22 or later:

```bash
npm ci
cp .env.example .env.local
npm run dev
```

Open <http://127.0.0.1:3000>. Against `aios dev`, which has no sign-in, you go straight in. Against
`aios api`, sign in with an API key, or with an identity provider if `OIDC_ISSUER` is set. The
Keycloak realm in `deploy/compose` has a client for this, `aios-console`, and test users.

Use `127.0.0.1` rather than `localhost`: the sign-in redirects and the cookie are tied to the
address in `CONSOLE_URL`.

## What it takes care of

- **Credentials stay on the server.** A token or key is sealed in an `httpOnly` cookie, so scripts
  on the page can't read it.
- **Anything an agent read can be hostile.** A summary, rationale or payload can carry an attacker's
  text, so React renders it all as text. Nothing here sets HTML from data. The page says when a
  request follows untrusted content, and calls a rationale what it is: a claim.
- **Addresses come from configuration.** Redirects use `CONSOLE_URL`, not a request's `Host` header.
- **Forms work before the page has loaded.** `Referrer-Policy: same-origin`, not `no-referrer`: with
  that, a form sent before hydration carries `Origin: null`, and Next refuses it as cross-site.
- **The graph is a picture of the list.** React Flow's own keyboard handling is off; the steps are a
  list of buttons beside it, which a keyboard and a screen reader can use.

## Checks

```bash
npm run lint
npm run typecheck
npm test
npm run build
```

`tests/unit/api/test_approvals_api.py` keeps the fields in `src/lib/types.ts` honest from the API's
side.

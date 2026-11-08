# @aios/client

The AIOS API for TypeScript and JavaScript: start and follow runs, answer approvals, do work
assigned to people, and say what you thought of a run. No dependencies: it uses the platform's
`fetch`, so it runs in Node 20 or later, in Deno, Bun and in browsers.

```ts
import { AIOS } from "@aios/client";

const aios = new AIOS({ server: "https://aios.example.com", token: process.env.AIOS_API_KEY });

const run = await aios.ask("ai-researcher", "What changed in OpenTelemetry in 2026?");
console.log(run.status, run.output);
```

Everything goes through the API with your own credential, so a program can see and do what that
credential may, and nothing more. A local `aios dev` server needs no token.

## Runs

```ts
const started = await aios.startRun("refund-desk", "Refund order 1001 $120");

for await (const event of aios.follow("refund-desk", started.run_id)) {
  console.log(event.type); // as it happens, until the run ends
}

const run = await aios.run("refund-desk", started.run_id);
```

`follow` reads the run's events as they happen. If the connection drops, it carries on from the last
event it saw. `ask` starts a run, waits for it to end and returns it. A run that waits for a person
waits as long as they take: pass an `AbortSignal` to stop waiting.

`startRun` takes a chat message, or `{ input }` for the workflow's own input, and optionally the
`data_class` of what you send.

## Approvals

```ts
for (const approval of await aios.approvals()) {
  console.log(approval.summary, approval.preview, approval.risk);
}

await aios.approve(id, { comment: "Checked the order" });
await aios.approve(id, { edits: { amountUSD: 60 }, comment: "Half: it arrived late" });
await aios.deny(id, { comment: "Outside the return window" });
```

A change made when approving is checked against the policies again, and refused if it would need
more approval than yours. `approval.rationale` is the agent's claim, not a fact.

## Work for people

```ts
for (const task of await aios.tasks()) {
  await aios.completeTask(task.task, { verdict: "sign" }, { comment: "Standard terms" });
}
```

## Feedback

```ts
await aios.feedback("ai-researcher", runId, {
  rating: "bad",
  correction: { answer: "France" },
  comment: "Wrong country",
  remember: true, // keep it in the App's memory too
});
```

## Errors

A refusal is an `AIOSError`: `status` is the HTTP status (403 for something your roles don't allow,
422 for a request AIOS can't take, 409 for something already answered), and `message` is AIOS's
reason. A status of 0 means nothing answered at the server address.

## Schemas

`aios.schemas()` lists every event type and manifest kind, and `aios.eventSchema("approval.granted")`
gives one event's payload as a JSON Schema.

## Developing

```bash
npm ci
npm run typecheck
npm test
npm run build
```

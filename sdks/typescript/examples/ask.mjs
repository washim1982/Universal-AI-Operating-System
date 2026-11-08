// Ask an App something, print its events as they happen, then its answer.
//
//   npm run build
//   node examples/ask.mjs <app> "<message>"
//
// Reads AIOS_SERVER (default http://127.0.0.1:18080) and, for a server with sign-in, AIOS_API_KEY.
import { AIOS } from "../dist/index.js";

const [app, message] = process.argv.slice(2);
if (!app || !message) {
  console.error('usage: node examples/ask.mjs <app> "<message>"');
  process.exit(2);
}

const aios = new AIOS({
  server: process.env.AIOS_SERVER ?? "http://127.0.0.1:18080",
  token: process.env.AIOS_API_KEY,
});

const started = await aios.startRun(app, message);
console.log(`run ${started.run_id}`);
for await (const event of aios.follow(app, started.run_id)) {
  console.log(`${event.time.slice(11, 23)} ${event.type}`);
}
const run = await aios.run(app, started.run_id);
console.log(JSON.stringify(run.output, null, 2));
console.log(`status=${run.status} cost=$${(run.usage?.cost_usd ?? 0).toFixed(4)}`);
process.exit(run.status === "completed" ? 0 : 1);

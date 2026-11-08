import { describe, expect, it } from "vitest";

import { AIOS, AIOSError, serverSentEvents } from "../src/index.js";

interface Sent {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

/** A stand-in for AIOS: answers each request in turn, and keeps what was sent. */
function server(...answers: (Response | (() => Response))[]) {
  const sent: Sent[] = [];
  const fetcher = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    sent.push({
      url: String(input),
      method: init?.method ?? "GET",
      headers: { ...(init?.headers as Record<string, string>) },
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    const next = answers.shift();
    if (!next) throw new Error("no more answers");
    return typeof next === "function" ? next() : next;
  };
  return { fetcher: fetcher as typeof fetch, sent };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function stream(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

function frame(position: number, type: string, data: unknown = {}): string {
  const event = { id: `e${position}`, type, source: "aios://acme", time: "now", tenantid: "acme", appid: "desk", principal: "system:x", data };
  return `id: ${position}\nevent: ${type}\ndata: ${JSON.stringify(event)}\n\n`;
}

describe("the client", () => {
  it("sends the caller's credential and tenant", async () => {
    const { fetcher, sent } = server(json({ principal: "service:ann@acme", tenant: "acme", roles: ["member"] }));
    const aios = new AIOS({ server: "https://aios.example/", token: "aios_key", tenant: "acme", fetch: fetcher });
    expect((await aios.me()).principal).toBe("service:ann@acme");
    expect(sent[0]?.url).toBe("https://aios.example/v1/me");
    expect(sent[0]?.headers.Authorization).toBe("Bearer aios_key");
    expect(sent[0]?.headers["X-AIOS-Tenant"]).toBe("acme");
  });

  it("starts a run with a message, or with the workflow's input", async () => {
    const started = { run_id: "01RUN", app: "desk", events: "/v1/apps/desk/runs/01RUN/events" };
    const { fetcher, sent } = server(json(started, 202), json(started, 202));
    const aios = new AIOS({ server: "http://aios", fetch: fetcher });
    await aios.startRun("desk", "Refund order 1001 $120");
    await aios.startRun("desk", { input: { order: "1001" }, data_class: "confidential" });
    expect(sent.map((s) => [s.method, s.url, s.body])).toEqual([
      ["POST", "http://aios/v1/apps/desk/runs", { message: "Refund order 1001 $120" }],
      ["POST", "http://aios/v1/apps/desk/runs", { input: { order: "1001" }, data_class: "confidential" }],
    ]);
    expect(sent[0]?.headers["Content-Type"]).toBe("application/json");
  });

  it("follows a run to its end, and picks up where a dropped stream was", async () => {
    const { fetcher, sent } = server(
      stream(frame(1, "run.started"), frame(2, "step.started").slice(0, 20)),
      stream(frame(2, "step.started"), frame(3, "run.completed"), frame(4, "never.read")),
    );
    const aios = new AIOS({ server: "http://aios", fetch: fetcher });
    const types: string[] = [];
    for await (const event of aios.follow("desk", "01RUN")) types.push(event.type);
    expect(types).toEqual(["run.started", "step.started", "run.completed"]);
    expect(sent.map((s) => s.headers["Last-Event-ID"])).toEqual(["0", "1"]);
    expect(sent[0]?.headers.Accept).toBe("text/event-stream");
  });

  it("asks: starts, waits, and returns the finished run", async () => {
    const finished = { run_id: "01RUN", app: "desk", status: "completed", output: { reply: "done" } };
    const { fetcher, sent } = server(
      json({ run_id: "01RUN", app: "desk", events: "" }, 202),
      stream(frame(1, "run.started"), frame(2, "run.completed")),
      json(finished),
    );
    const run = await new AIOS({ server: "http://aios", fetch: fetcher }).ask("desk", "hi");
    expect(run.output).toEqual({ reply: "done" });
    expect(sent.map((s) => s.url)).toEqual([
      "http://aios/v1/apps/desk/runs",
      "http://aios/v1/apps/desk/runs/01RUN/events",
      "http://aios/v1/apps/desk/runs/01RUN",
    ]);
  });

  it("answers approvals, with changes, and does tasks", async () => {
    const { fetcher, sent } = server(json({}), json({}), json({}), json({}), json({}));
    const aios = new AIOS({ server: "http://aios", fetch: fetcher });
    await aios.approvals({ status: "granted", everyone: true });
    await aios.approve("01APP", { comment: "half", edits: { amountUSD: 60 } });
    await aios.deny("01APP");
    await aios.completeTask("01TSK", { verdict: "sign" }, { comment: "standard" });
    await aios.feedback("desk", "01RUN", { rating: "bad", correction: { answer: "France" }, remember: true });
    expect(sent.map((s) => [s.method, s.url.replace("http://aios", ""), s.body])).toEqual([
      ["GET", "/v1/approvals?status=granted&everyone=true", undefined],
      ["POST", "/v1/approvals/01APP/approve", { comment: "half", edits: { amountUSD: 60 } }],
      ["POST", "/v1/approvals/01APP/deny", {}],
      ["POST", "/v1/tasks/01TSK/complete", { output: { verdict: "sign" }, comment: "standard" }],
      ["POST", "/v1/apps/desk/runs/01RUN/feedback", { rating: "bad", correction: { answer: "France" }, remember: true }],
    ]);
  });

  it("names its parts safely in paths", async () => {
    const { fetcher, sent } = server(json({}));
    await new AIOS({ server: "http://aios", fetch: fetcher }).eventSchema("../keys?x=1");
    expect(sent[0]?.url).toBe("http://aios/v1/schemas/events/..%2Fkeys%3Fx%3D1");
  });

  it("says why AIOS refused", async () => {
    const { fetcher } = server(json({ detail: "the action has no account" }, 422), json({ detail: [{ msg: "x" }] }, 400));
    const aios = new AIOS({ server: "http://aios", fetch: fetcher });
    const refused = await aios.approve("01APP", { edits: { account: "mine" } }).catch((e: unknown) => e);
    expect(refused).toBeInstanceOf(AIOSError);
    expect((refused as AIOSError).status).toBe(422);
    expect((refused as AIOSError).message).toBe("the action has no account");
    const other = (await aios.apps().catch((e: unknown) => e)) as AIOSError;
    expect([other.status, other.message]).toEqual([400, "AIOS answered 400"]);
  });

  it("says when nothing answered", async () => {
    const fetcher = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch;
    const error = (await new AIOS({ server: "http://aios", fetch: fetcher }).apps().catch((e: unknown) => e)) as AIOSError;
    expect(error.status).toBe(0);
    expect(error.message).toContain("no answer from AIOS at http://aios");
  });
});

describe("server-sent events", () => {
  it("reads events split anywhere, with several data lines and comments", async () => {
    const text = ": hello\r\nid: 7\r\nevent: x\r\ndata: one\r\ndata: two\r\n\r\nid: 8\ndata: {}\n\n";
    const chunks = [text.slice(0, 5), text.slice(5, 23), text.slice(23)];
    const found = [];
    for await (const event of serverSentEvents(stream(...chunks).body!)) found.push(event);
    expect(found).toEqual([
      { id: "7", event: "x", data: "one\ntwo" },
      { id: "8", data: "{}" },
    ]);
  });
});

import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { createHmac, randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import worker, { AlertLedger } from "../src/index.js";

const SECRET = "test-secret-a";
const TOKEN = "test-agent-token";
const START = 1_800_000_000_000;
let logs;

beforeEach((t) => {
  logs = [];
  for (const level of ["log", "error", "warn", "info", "debug"]) {
    t.mock.method(console, level, (...args) => logs.push({ level, args }));
  }
  t.mock.method(globalThis, "fetch", () => { throw new Error("Unexpected fetch in local test"); });
});

class MemoryStorage {
  values = new Map();
  alarm = null;
  writes = [];

  async get(key) { return structuredClone(this.values.get(key)); }
  async put(key, value) {
    this.values.set(key, structuredClone(value));
    this.writes.push(structuredClone(value));
  }
  async setAlarm(at) { this.alarm = Number(at); }
  async getAlarm() { return this.alarm; }
  async deleteAlarm() { this.alarm = null; }
  async transaction(work) {
    const values = structuredClone(this.values);
    const alarm = this.alarm;
    try { return await work(this); }
    catch (error) {
      this.values = values;
      this.alarm = alarm;
      throw error;
    }
  }
}

function signature(body, timestamp, secret = SECRET) {
  return `v1=${createHmac("sha256", secret).update(`v1:${timestamp}:`).update(body).digest("hex")}`;
}

function harness(t, overrides = {}, notify = () => Response.json({ id: "test-notification-id" }, { status: 201 })) {
  let clock = START;
  const calls = [];
  const objects = new Map();
  const env = {
    NOTIFY_WEBHOOK_SECRET: SECRET,
    NOTIFY_API_TOKEN: TOKEN,
    NOTIFY_DEVICE_ID: "test-device",
    NOW: () => clock,
    ...overrides,
  };
  env.LEDGER = {
    idFromName: (id) => id,
    get(id) {
      if (!objects.has(id)) {
        const storage = new MemoryStorage();
        objects.set(id, { storage, instance: new AlertLedger({ storage }, env) });
      }
      return { fetch: (request) => objects.get(id).instance.fetch(request) };
    },
  };
  t.mock.method(globalThis, "fetch", async (url, init) => {
    calls.push({ url, ...init });
    return notify(url, init, calls.length);
  });
  return {
    env, calls, objects,
    get clock() { return clock; },
    setClock(value) { clock = value; },
    async post(body = JSON.stringify({ incident: randomUUID() }), options = {}) {
      const timestamp = options.timestamp ?? String(Math.floor(clock / 1000));
      const headers = new Headers({
        "Content-Type": "application/json",
        "X-Safety-Timestamp": timestamp,
        "X-Safety-Signature": options.signature ?? signature(body, timestamp, options.secret ?? SECRET),
      });
      for (const [name, value] of Object.entries(options.headers ?? {})) {
        if (value === null) headers.delete(name);
        else headers.set(name, value);
      }
      const method = options.method ?? "POST";
      const request = new Request(options.url ?? "https://receiver.invalid/alert", {
        method, headers, ...(method === "GET" || method === "HEAD" ? {} : { body }),
      });
      return worker.fetch(request, env);
    },
    async record(incident) { return objects.get(incident).storage.get("record"); },
    async alarm(incident) {
      const { storage, instance } = objects.get(incident);
      const at = await storage.getAlarm();
      assert.notEqual(at, null);
      clock = at;
      await storage.deleteAlarm();
      await instance.alarm();
    },
    restart(incident) {
      const entry = objects.get(incident);
      entry.instance = new AlertLedger({ storage: entry.storage }, env);
    },
  };
}

async function expectError(response, status, error) {
  assert.equal(response.status, status);
  assert.equal(await response.text(), JSON.stringify({ error }));
}

test("valid signature queues once and sends only the fixed notification fields", async (t) => {
  const incident = randomUUID();
  const body = JSON.stringify({ incident });
  const h = harness(t, {}, async () => {
    const pending = await h.record(incident);
    assert.equal(pending.status, "pending");
    assert.equal(pending.attempts, 1);
    assert.notEqual(await h.objects.get(incident).storage.getAlarm(), null);
    return Response.json({ id: "test-notification-id" }, { status: 201 });
  });
  const response = await h.post(body, { headers: { "X-Test-Marker": "inert-request-marker" } });
  assert.equal(response.status, 202);
  assert.deepEqual(await response.json(), { incident, status: "sent" });
  assert.equal(h.calls.length, 1);
  const call = h.calls[0];
  assert.equal(call.url, "https://notify.belweave.ai/api/agent/notifications");
  assert.equal(call.method, "POST");
  assert.deepEqual(call.headers, {
    Authorization: `Bearer ${TOKEN}`,
    "Content-Type": "application/json",
    "Idempotency-Key": `b300-safety-${incident}`,
  });
  assert.equal(call.redirect, "manual");
  assert.ok(call.signal instanceof AbortSignal);
  assert.deepEqual(JSON.parse(call.body), {
    title: "Child-safety block fired",
    summary: `Incident ${incident}. File a CyberTipline report.`,
    body: `A child-safety hard-block fired on the B300 inference gateway. Incident ID: ${incident}. ` +
      `The offending key was revoked automatically and sealed evidence is in the b300-evidence bucket under incidents/${incident}.json. ` +
      "Report to NCMEC's CyberTipline (https://report.cybertip.org/) as required by 18 U.S.C. 2258A. " +
      "Do not open or share the evidence outside the reporting process.",
    project: "B300 safety", deviceIds: ["test-device"],
  });
  assert.deepEqual(await h.record(incident), {
    incident, status: "sent", attempts: 1, first_seen: START,
    notified_at: START, notification_id: "test-notification-id",
  });
  const initial = h.objects.get(incident).storage.writes[0];
  assert.equal(initial.status, "pending");
  assert.equal(initial.attempts, 0);
  assert.equal(await h.objects.get(incident).storage.getAlarm(), null);
});

test("unset device uses account defaults and origin is configurable", async (t) => {
  const h = harness(t, { NOTIFY_DEVICE_ID: undefined, NOTIFY_API_ORIGIN: "https://notify.invalid/" });
  assert.equal((await h.post()).status, 202);
  assert.equal(h.calls[0].url, "https://notify.invalid/api/agent/notifications");
  assert.equal(Object.hasOwn(JSON.parse(h.calls[0].body), "deviceIds"), false);
});

for (const status of [200, 202, 204]) {
  test(`Notify ${status} with no notification id is success`, async (t) => {
    const h = harness(t, {}, () => new Response(null, { status }));
    const incident = randomUUID();
    assert.equal((await (await h.post(JSON.stringify({ incident }))).json()).status, "sent");
    assert.equal(Object.hasOwn(await h.record(incident), "notification_id"), false);
  });
}

const authCases = [
  ["bad signature", { signature: `v1=${"0".repeat(64)}` }],
  ["wrong length", { signature: "v1=abcd" }],
  ["long signature", { signature: `v1=${"0".repeat(66)}` }],
  ["uppercase signature", { signature: `v1=${"A".repeat(64)}` }],
  ["non-hex signature", { signature: `v1=${"z".repeat(64)}` }],
  ["wrong version", { signature: `v2=${"0".repeat(64)}` }],
  ["missing timestamp", { headers: { "X-Safety-Timestamp": null } }],
  ["missing signature", { headers: { "X-Safety-Signature": null } }],
  ["missing both headers", { headers: { "X-Safety-Timestamp": null, "X-Safety-Signature": null } }],
  ["unrelated secret", { secret: "test-secret-other" }],
  ["stale timestamp", { timestamp: String(START / 1000 - 301) }],
  ["future timestamp", { timestamp: String(START / 1000 + 301) }],
  ["fractional timestamp", { timestamp: `${START / 1000}.0` }],
  ["negative timestamp", { timestamp: "-1" }],
  ["timestamp with a plus", { timestamp: `+${START / 1000}` }],
  ["exponential timestamp", { timestamp: "18e8" }],
  ["non-ASCII timestamp", { timestamp: "¹800000000" }],
  ["unsafe integer timestamp", { timestamp: "9007199254740992" }],
];
for (const [name, options] of authCases) {
  test(`auth rejects ${name} with the same fixed response`, async (t) => {
    const h = harness(t);
    await expectError(await h.post(undefined, options), 401, "unauthorized");
    assert.equal(h.calls.length, 0);
    assert.equal(h.objects.size, 0);
    assert.deepEqual(logs.map(({ args }) => JSON.parse(args[0])), [{ event: "rejected", reason: "auth" }]);
  });
}

test("legacy bearer auth alone is never accepted", async (t) => {
  const h = harness(t);
  await expectError(await h.post(undefined, { headers: {
    Authorization: `Bearer ${TOKEN}`, "X-Safety-Timestamp": null, "X-Safety-Signature": null,
  } }), 401, "unauthorized");
});

test("both rotation secrets work, including the second", async (t) => {
  const h = harness(t, { NOTIFY_WEBHOOK_SECRET: `${SECRET} test-secret-b` });
  for (const secret of [SECRET, "test-secret-b"]) {
    assert.equal((await h.post(undefined, { secret })).status, 202);
  }
  assert.equal(h.calls.length, 2);
});

test("replay boundary is inclusive and configurable", async (t) => {
  const h = harness(t);
  for (const delta of [-300, 300]) {
    assert.equal((await h.post(undefined, { timestamp: String(START / 1000 + delta) })).status, 202);
  }
  h.env.REPLAY_WINDOW_S = "10";
  assert.equal((await h.post(undefined, { timestamp: String(START / 1000 - 10) })).status, 202);
  await expectError(await h.post(undefined, { timestamp: String(START / 1000 - 11) }), 401, "unauthorized");
});

test("replay window does not round away subsecond expiry", async (t) => {
  const h = harness(t);
  h.setClock(START + 1);
  await expectError(await h.post(undefined, { timestamp: String(START / 1000 - 300) }), 401, "unauthorized");
});

test("signature uses original timestamp digits and exact body bytes", async (t) => {
  const h = harness(t);
  const incident = randomUUID();
  const raw = ` { "incident" : "${incident}" }\n`;
  assert.equal((await h.post(raw, { timestamp: `0${START / 1000}` })).status, 202);
  await expectError(await h.post(raw, {
    signature: signature(JSON.stringify({ incident }), String(START / 1000)),
  }), 401, "unauthorized");
});

test("auth happens before JSON parsing", async (t) => {
  const h = harness(t);
  await expectError(await h.post("inert-invalid-json", { signature: `v1=${"0".repeat(64)}` }), 401, "unauthorized");
});

const badBodies = [
  ["extra field", () => JSON.stringify({ incident: randomUUID(), extra: "inert-marker" })],
  ["non-UUID incident", () => JSON.stringify({ incident: "inert-marker" })],
  ["uppercase UUID", () => JSON.stringify({ incident: randomUUID().replace(/^./, "a").toUpperCase() })],
  ["wrong UUID version", () => JSON.stringify({ incident: randomUUID().replace(/^(.{14})./, (_, prefix) => `${prefix}5`) })],
  ["wrong UUID variant", () => JSON.stringify({ incident: randomUUID().replace(/^(.{19})./, (_, prefix) => `${prefix}0`) })],
  ["UUID with trailing newline", () => JSON.stringify({ incident: `${randomUUID()}\n` })],
  ["wrong type", () => '{"incident":42}'],
  ["null incident", () => '{"incident":null}'],
  ["array", () => JSON.stringify([{ incident: randomUUID() }])],
  ["empty object", () => "{}"],
  ["null", () => "null"],
  ["string", () => '"inert-marker"'],
  ["malformed JSON", () => "{"],
  ["empty body", () => ""],
  ["invalid UTF-8", () => Buffer.from([0xff])],
];
for (const [name, body] of badBodies) {
  test(`valid signature rejects ${name}`, async (t) => {
    const h = harness(t);
    await expectError(await h.post(body()), 400, "bad_request");
    assert.equal(h.calls.length, 0);
    assert.equal(h.objects.size, 0);
  });
}

test("body limit counts bytes, checks declared and actual length, and allows 256", async (t) => {
  const h = harness(t);
  const body = JSON.stringify({ incident: randomUUID() });
  assert.equal((await h.post(body.padEnd(256))).status, 202);
  for (const options of [{}, { headers: { "Content-Length": "1" } }]) {
    await expectError(await h.post(body.padEnd(257), options), 413, "payload_too_large");
  }
  await expectError(await h.post("é".repeat(129)), 413, "payload_too_large");
  await expectError(await h.post(body, { headers: { "Content-Length": "257" } }), 413, "payload_too_large");
});

test("body reader cancels on overflow without reading the remaining chunks", async (t) => {
  const h = harness(t);
  let pulls = 0;
  let cancelled = false;
  const stream = new ReadableStream({
    pull(controller) { pulls++; controller.enqueue(new Uint8Array(128)); },
    cancel() { cancelled = true; },
  }, { highWaterMark: 0 });
  const request = new Request("https://receiver.invalid/alert", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: stream, duplex: "half",
  });
  await expectError(await worker.fetch(request, h.env), 413, "payload_too_large");
  assert.equal(pulls, 3);
  assert.equal(cancelled, true);
});

test("declared oversize cancels the body without reading it", async (t) => {
  const h = harness(t);
  let pulls = 0;
  const request = new Request("https://receiver.invalid/alert", {
    method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "257" },
    body: new ReadableStream({ pull() { pulls++; } }, { highWaterMark: 0 }), duplex: "half",
  });
  await expectError(await worker.fetch(request, h.env), 413, "payload_too_large");
  assert.equal(pulls, 0);
});

test("requires JSON content type, permits charset, and checks method and path", async (t) => {
  const h = harness(t);
  for (const type of [null, "text/plain", "application/jsonp"]) {
    await expectError(await h.post(undefined, { headers: { "Content-Type": type } }), 415, "unsupported_media_type");
  }
  assert.equal((await h.post(undefined, { headers: { "Content-Type": "application/json; charset=utf-8" } })).status, 202);
  await expectError(await h.post(undefined, { url: "https://receiver.invalid/unknown" }), 404, "not_found");
  await expectError(await h.post(undefined, { url: "https://receiver.invalid/alert/" }), 404, "not_found");
  for (const method of ["GET", "PUT", "DELETE", "OPTIONS"]) {
    const response = await h.post(undefined, { method });
    assert.equal(response.headers.get("Allow"), "POST");
    await expectError(response, 405, "method_not_allowed");
  }
  const health = await h.post(undefined, { method: "GET", url: "https://receiver.invalid/health" });
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { ok: true });
});

for (const [name, secret] of [
  ["missing", undefined], ["empty", ""], ["whitespace", " "],
  ["three secrets", "test-a test-b test-c"], ["double space", "test-a  test-b"],
  ["trailing newline", "test-a\n"],
]) {
  test(`${name} secret config fails closed on every route`, async (t) => {
    const h = harness(t, { NOTIFY_WEBHOOK_SECRET: secret });
    for (const options of [{}, { method: "GET" },
      { url: "https://receiver.invalid/unknown" }, { method: "GET", url: "https://receiver.invalid/health" }]) {
      await expectError(await h.post(undefined, options), 503, "unavailable");
    }
    assert.equal(h.calls.length, 0);
  });
}

test("repeated incident remains deduplicated after object reconstruction", async (t) => {
  const h = harness(t);
  const incident = randomUUID();
  const body = JSON.stringify({ incident });
  assert.equal((await h.post(body)).status, 202);
  h.restart(incident);
  const repeat = await h.post(body);
  assert.equal(repeat.status, 200);
  assert.deepEqual(await repeat.json(), { incident, status: "sent" });
  await h.objects.get(incident).instance.alarm();
  assert.equal(h.calls.length, 1);
});

test("concurrent double POST sends once while Notify is in flight", async (t) => {
  const entered = Promise.withResolvers();
  const release = Promise.withResolvers();
  const h = harness(t, {}, async () => {
    entered.resolve();
    await release.promise;
    return Response.json({ id: "test-notification-id" }, { status: 201 });
  });
  const body = JSON.stringify({ incident: randomUUID() });
  const first = h.post(body);
  const second = h.post(body);
  await entered.promise;
  assert.equal(h.calls.length, 1);
  release.resolve();
  const responses = await Promise.all([first, second]);
  assert.deepEqual(responses.map((response) => response.status).sort(), [200, 202]);
  assert.equal(h.calls.length, 1);
});

test("Notify 500 stays pending, duplicate POST does not retry, and alarm succeeds", async (t) => {
  const h = harness(t, {}, (_url, _init, count) => new Response(null, { status: count === 1 ? 500 : 201 }));
  const incident = randomUUID();
  const body = JSON.stringify({ incident });
  const response = await h.post(body);
  assert.equal(response.status, 202);
  assert.deepEqual(await response.json(), { incident, status: "queued" });
  const repeat = await h.post(body);
  assert.equal(repeat.status, 200);
  assert.deepEqual(await repeat.json(), { incident, status: "pending" });
  assert.equal(h.calls.length, 1);
  assert.equal(await h.objects.get(incident).storage.getAlarm(), START + 30_000);
  h.restart(incident);
  await h.alarm(incident);
  assert.equal(h.calls.length, 2);
  assert.equal((await h.record(incident)).status, "sent");
  assert.equal((await h.record(incident)).notified_at, START + 30_000);
  assert.equal(h.calls[0].headers["Idempotency-Key"], h.calls[1].headers["Idempotency-Key"]);
});

for (const status of [400, 401, 403, 404, 429, 503, 302]) {
  test(`Notify ${status} is retried${status < 405 && status >= 400 ? " and logged as a config error" : ""}`, async (t) => {
    const h = harness(t, {}, () => new Response(null, { status }));
    const incident = randomUUID();
    await h.post(JSON.stringify({ incident }));
    assert.equal((await h.record(incident)).status, "pending");
    await h.alarm(incident);
    assert.equal(h.calls.length, 2);
    const event = [400, 401, 403, 404].includes(status) ? "notify_config_error" : "notify_failed";
    assert.equal(JSON.parse(logs[0].args[0]).event, event);
    assert.equal(logs[0].level, "error");
  });
}

test("network errors retry without logging exception text", async (t) => {
  const h = harness(t, {}, () => { throw new Error("inert-private-marker"); });
  const incident = randomUUID();
  await h.post(JSON.stringify({ incident }));
  await h.alarm(incident);
  assert.equal(h.calls.length, 2);
  assert.equal(JSON.parse(logs[0].args[0]).status, "network");
  assert.ok(!JSON.stringify(logs).includes("inert-private-marker"));
});

test("each attempt aborts after ten seconds and then schedules a retry", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const entered = Promise.withResolvers();
  const h = harness(t, {}, (_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener("abort", () => reject(new Error("inert-abort")), { once: true });
    entered.resolve();
  }));
  const incident = randomUUID();
  const result = h.post(JSON.stringify({ incident }));
  await entered.promise;
  t.mock.timers.tick(9999);
  assert.equal(h.calls[0].signal.aborted, false);
  t.mock.timers.tick(1);
  assert.equal(h.calls[0].signal.aborted, true);
  assert.equal((await result).status, 202);
  assert.equal(JSON.parse(logs[0].args[0]).status, "timeout");
  assert.equal(await h.objects.get(incident).storage.getAlarm(), START + 30_000);
});

test("backoff doubles to the cap, then gives up after twelve attempts", async (t) => {
  const h = harness(t, {}, () => new Response(null, { status: 500 }));
  const incident = randomUUID();
  const body = JSON.stringify({ incident });
  await h.post(body);
  for (const seconds of [30, 60, 120, 240, 480, 960, 1800, 1800, 1800, 1800, 1800]) {
    assert.equal(await h.objects.get(incident).storage.getAlarm() - h.clock, seconds * 1000);
    await h.alarm(incident);
  }
  assert.equal(h.calls.length, 12);
  assert.equal((await h.record(incident)).status, "failed");
  assert.equal((await h.record(incident)).attempts, 12);
  assert.equal(await h.objects.get(incident).storage.getAlarm(), null);
  const last = logs.at(-1);
  assert.equal(last.level, "error");
  assert.deepEqual(JSON.parse(last.args[0]), { event: "notify_gave_up", incident, attempt: 12, status: "failed" });
  const repeat = await h.post(body);
  assert.equal(repeat.status, 200);
  assert.equal((await repeat.json()).status, "failed");
  await h.objects.get(incident).instance.alarm();
  assert.equal(h.calls.length, 12);
});

test("MAX_ATTEMPTS includes the immediate attempt", async (t) => {
  const h = harness(t, { MAX_ATTEMPTS: "2" }, () => new Response(null, { status: 500 }));
  const incident = randomUUID();
  await h.post(JSON.stringify({ incident }));
  await h.alarm(incident);
  assert.equal((await h.record(incident)).status, "failed");
  assert.equal(h.calls.length, 2);
  assert.equal(await h.objects.get(incident).storage.getAlarm(), null);
});

test("missing Notify token persists pending work and retries after config is fixed", async (t) => {
  const h = harness(t, { NOTIFY_API_TOKEN: "" });
  const incident = randomUUID();
  await h.post(JSON.stringify({ incident }));
  assert.equal(h.calls.length, 0);
  assert.equal((await h.record(incident)).status, "pending");
  assert.equal(JSON.parse(logs[0].args[0]).event, "notify_config_error");
  h.env.NOTIFY_API_TOKEN = TOKEN;
  await h.alarm(incident);
  assert.equal((await h.record(incident)).status, "sent");
});

test("a duplicate alarm before the retry time does not send early", async (t) => {
  const h = harness(t, {}, () => new Response(null, { status: 500 }));
  const incident = randomUUID();
  await h.post(JSON.stringify({ incident }));
  await h.objects.get(incident).instance.alarm();
  assert.equal(h.calls.length, 1);
  assert.equal(await h.objects.get(incident).storage.getAlarm(), START + 30_000);
});

test("recovery alarm resumes persisted pending work after an interrupted attempt", async (t) => {
  const h = harness(t);
  const incident = randomUUID();
  h.env.LEDGER.get(incident);
  const { storage } = h.objects.get(incident);
  await storage.put("record", { incident, status: "pending", attempts: 1, first_seen: START, retry_at: START + 40_000 });
  await storage.setAlarm(START + 40_000);
  h.restart(incident);
  await h.alarm(incident);
  assert.equal((await h.record(incident)).status, "sent");
  assert.equal((await h.record(incident)).attempts, 2);
  assert.equal(h.calls[0].headers["Idempotency-Key"], `b300-safety-${incident}`);
});

test("recovery after an interrupted last attempt marks failed without exceeding the limit", async (t) => {
  const h = harness(t, { MAX_ATTEMPTS: "1" });
  const incident = randomUUID();
  h.env.LEDGER.get(incident);
  const { storage } = h.objects.get(incident);
  await storage.put("record", { incident, status: "pending", attempts: 1, first_seen: START, retry_at: START });
  await storage.setAlarm(START);
  await h.alarm(incident);
  assert.equal((await h.record(incident)).status, "failed");
  assert.equal(h.calls.length, 0);
  assert.equal(JSON.parse(logs.at(-1).args[0]).event, "notify_gave_up");
});

test("logs contain only structured allowlisted metadata and validated incidents", async (t) => {
  const h = harness(t, { MAX_ATTEMPTS: "2" }, (_url, _init, count) => {
    if (count <= 2) throw new Error(`${SECRET} ${TOKEN} inert-private-marker`);
    return Response.json({ id: "inert-notification-id" }, { status: 201 });
  });
  const incident = randomUUID();
  const body = JSON.stringify({ incident });
  const signed = signature(body, String(START / 1000));
  await h.post(body);
  await h.alarm(incident);
  const next = randomUUID();
  await h.post(JSON.stringify({ incident: next }));
  await h.post(JSON.stringify({ incident: "inert-invalid-incident", extra: "inert-marker" }));
  await h.post(body, { signature: `v1=${"0".repeat(64)}` });
  const text = logs.map(({ args }) => args.join(" ")).join("\n");
  for (const forbidden of [SECRET, TOKEN, signed, body, "inert-private-marker", "inert-invalid-incident", "inert-marker", "inert-notification-id"]) {
    assert.equal(text.includes(forbidden), false);
  }
  for (const { args } of logs) {
    assert.equal(args.length, 1);
    const entry = JSON.parse(args[0]);
    assert.ok(Object.keys(entry).every((key) => ["event", "reason", "incident", "attempt", "status"].includes(key)));
    if (entry.incident) assert.ok([incident, next].includes(entry.incident));
  }
});

test("sign helper prints verifiable headers, takes secrets only from env, and uses the first rotation key", async (t) => {
  const h = harness(t, { NOTIFY_WEBHOOK_SECRET: `${SECRET} test-secret-b` });
  const body = JSON.stringify({ incident: randomUUID() });
  const helper = new URL("../tools/sign.mjs", import.meta.url);
  const result = spawnSync(process.execPath, [helper.pathname, body], {
    env: { NOTIFY_WEBHOOK_SECRET: `${SECRET} test-secret-b` }, encoding: "utf8",
  });
  assert.equal(result.status, 0);
  assert.equal(result.stderr, "");
  assert.ok(!result.stdout.includes(SECRET));
  const [timestampLine, signatureLine] = result.stdout.trim().split("\n");
  const timestamp = timestampLine.slice("X-Safety-Timestamp: ".length);
  const signed = signatureLine.slice("X-Safety-Signature: ".length);
  assert.equal(signed, signature(body, timestamp));
  h.setClock(Number(timestamp) * 1000);
  assert.equal((await h.post(body, { timestamp, signature: signed })).status, 202);
  const missing = spawnSync(process.execPath, [helper.pathname, body, SECRET], { env: {}, encoding: "utf8" });
  assert.equal(missing.status, 1);
  assert.equal(missing.stdout, "");
  assert.ok(!missing.stderr.includes(SECRET));
});

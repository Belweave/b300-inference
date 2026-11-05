// Local workerd only: no Wrangler, account, remote bindings, or real Notify.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { parse } from "smol-toml";

assert.equal(process.argv.length, 2, "dry-run takes no arguments or remote targets");
const config = JSON.parse(JSON.stringify(parse(await readFile(new URL("../wrangler.toml", import.meta.url), "utf8"))));
assert.equal(config.workers_dev, false);
assert.equal(config.routes, undefined, "receiver must remain without an active route");
assert.equal(config.route, undefined, "receiver must remain without an active route");
assert.equal(config.vars.DRILL_MODE, undefined, "production config must not enable drill mode");
assert.deepEqual(config.durable_objects.bindings, [{ name: "LEDGER", class_name: "AlertLedger" }]);
assert.deepEqual(config.migrations, [{ tag: "v1", new_sqlite_classes: ["AlertLedger"] }]);

const secret = "offline-fixture-secret";
const webhook = "https://notify.invalid/hooks/offline-fixture";
const incident = "00000000-0000-4000-8000-000000000001";
const body = JSON.stringify({ incident });
const calls = [];
const storage = await mkdtemp(join(tmpdir(), "b300-alert-dry-run-"));
let runtime;

async function start(bindings) {
  runtime = new Miniflare({ ...convertV4MiniflareOptions({
    name: config.name,
    modules: true,
    scriptPath: fileURLToPath(new URL(`../${config.main}`, import.meta.url)),
    compatibilityDate: config.compatibility_date,
    host: "127.0.0.1",
    port: 0,
    cf: false, // Do not fetch Cloudflare's request metadata.
    durableObjects: { LEDGER: { className: "AlertLedger", useSQLite: true } },
    resourcePersistencePath: storage,
    bindings: { ...config.vars, ...bindings }, // Never read process.env or .dev.vars.
    outboundService: async (request) => {
      // Intercept ALL Worker egress. There is deliberately no fetch fallback.
      assert.equal(request.url, webhook);
      assert.equal(request.method, "POST");
      calls.push({ body: await request.text(), key: request.headers.get("Idempotency-Key") });
      return Response.json({ eventId: "offline-fixture-event" }, { status: 202 });
    },
  }), enableDevRegistry: false, telemetry: { enabled: false } });
  await runtime.ready;
}

async function check(path, init, status, expected) {
  const response = await runtime.dispatchFetch(`http://receiver.invalid${path}`, init);
  assert.equal(response.status, status);
  assert.deepEqual(await response.json(), expected);
}

function signed() {
  const timestamp = String(Math.floor(Date.now() / 1000));
  return {
    method: "POST", body,
    headers: {
      "Content-Type": "application/json",
      "X-Safety-Timestamp": timestamp,
      "X-Safety-Signature": `v1=${createHmac("sha256", secret).update(`v1:${timestamp}:${body}`).digest("hex")}`,
    },
  };
}

try {
  await start({});
  await check("/health", {}, 503, { error: "unavailable", drill_mode: false });
  await runtime.dispose();

  const bindings = { NOTIFY_WEBHOOK_SECRET: secret, NOTIFY_ALERT_WEBHOOK_URL: webhook };
  await start(bindings);
  await check("/health", {}, 200, { ok: true, drill_mode: false });
  await check("/alert", { method: "POST", body, headers: { "Content-Type": "application/json" } },
    401, { error: "unauthorized" });
  assert.equal(calls.length, 0);
  await check("/alert", signed(), 202, { incident, status: "sent" });
  await check("/alert", signed(), 200, { incident, status: "sent" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].key, `b300-safety-${incident}`);
  assert.deepEqual(Object.keys(JSON.parse(calls[0].body)), ["title", "summary", "body", "project"]);
  await runtime.dispose();

  // Restart the actual runtime, retaining only its temporary SQLite files.
  await start(bindings);
  await check("/alert", signed(), 200, { incident, status: "sent" });
  assert.equal(calls.length, 1, "restart must not deliver the incident twice");
  console.log("PASS: 6 local workerd checks (unconfigured/configured health, auth, accept, duplicate, restart).");
  console.log("Notify mocked; temporary SQLite storage only; receiver remains undeployed.");
} finally {
  try { await runtime?.dispose(); }
  finally { await rm(storage, { recursive: true, force: true }); }
}

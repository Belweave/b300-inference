# Draft safety alert receiver

A small, dependency-free Cloudflare Worker for incident-only alerts from the B300 inference gateway. It runs separately from the B300 node, so a node outage does not stop alert delivery. Nothing has been deployed. This draft does not change the gateway.

## Signature proposal for Netops

The gateway currently sends a bearer token, fire-and-forget. This receiver requires HMAC instead and will reject that existing request. Netops must agree to and implement this proposal before use.

POST `/alert` with `Content-Type: application/json` and exactly `{"incident":"<lowercase-v4-uuid>"}`. Sign the exact UTF-8 body bytes, including any whitespace:

- `X-Safety-Timestamp`: Unix seconds, ASCII digits only.
- `X-Safety-Signature`: `v1=` followed by the lowercase hex HMAC-SHA256 of `v1:<timestamp>:<rawBody>`, keyed by `NOTIFY_WEBHOOK_SECRET`.

Proposed gateway change, where `incident` comes from `crypto.randomUUID()`:

```js
const body = JSON.stringify({ incident }), ts = String(Math.floor(Date.now() / 1000)), enc = new TextEncoder();
const key = await crypto.subtle.importKey("raw", enc.encode(env.NOTIFY_WEBHOOK_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
const mac = await crypto.subtle.sign("HMAC", key, enc.encode(`v1:${ts}:${body}`));
const signature = "v1=" + Array.from(new Uint8Array(mac), b => b.toString(16).padStart(2, "0")).join("");
const response = await fetch(env.NOTIFY_WEBHOOK, { method: "POST", headers: { "Content-Type": "application/json", "X-Safety-Timestamp": ts, "X-Safety-Signature": signature }, body });
```

Netops should keep delivery alive with `ctx.waitUntil` and retry network failures or non-2xx responses a few times with backoff. Keep the same incident and body; refresh the timestamp and signature for each attempt. Receiver retries only start after it has durably accepted an incident; they cannot recover a gateway request that never arrived.

The default replay window is ±300 seconds, inclusive, set by `REPLAY_WINDOW_S`. The receiver accepts one secret or two separated by exactly one space. For rotation, install both on the receiver, switch the gateway to the new single secret, then remove the old receiver secret after the replay window and outstanding gateway retries. Verification uses WebCrypto HMAC verification over bytes before JSON parsing. All authentication failures return the same 401 body. Missing, empty, or malformed secret configuration returns 503 on every route, including health.

## Behaviour

Bodies are limited to 256 bytes, checked against both Content-Length and the stream. Invalid JSON or incident fields return 400; an oversized body returns 413; non-JSON content type returns 415. Only POST `/alert` is accepted (other methods: 405; unknown paths: 404). GET `/health` returns only `{"ok":true}`.

One `AlertLedger` Durable Object per incident stores pending work before trying Notify immediately. A new incident returns 202 with `sent` or `queued`; a duplicate returns 200 with its current `pending`, `sent`, or `failed` status and does not send again. Ledger entries are retained for deduplication. Stored `first_seen` and `notified_at` timestamps are Unix milliseconds.

Each Notify attempt has a ten-second timeout. Failures retry by alarm after 30 seconds, 60 seconds, 2 minutes, 4 minutes, and so on, capped at 30 minutes per delay. `MAX_ATTEMPTS` defaults to 12, including the immediate attempt. All non-2xx responses retry, including configuration errors (400/401/403/404), which produce an error log. Exhaustion persists `failed` and emits `notify_gave_up` at error level; fixing configuration alone does not restart a failed incident.

The ledger serializes POSTs and alarms. It saves attempt state and a recovery alarm together before contacting Notify. A restart during a delivery can repeat that delivery with the same `Idempotency-Key: b300-safety-<incident>`; Notify must honour that key to avoid a duplicate in this case. Any 2xx marks the incident sent; a response JSON `id`, when present, is stored privately.

Notify receives only the fixed reporting instructions and incident ID. No request content or key name is forwarded. `NOTIFY_DEVICE_ID` selects Preetham's iPhone; omitting it uses the defaults of Preetham's own account, whose agent token is used. Logs contain only structured event metadata and validated incident IDs, never credentials, signatures, request bodies, or Notify response bodies.

## Local tests and signing helper

Node 24, no package install:

```sh
node --test alert-receiver/test
make alert-test
```

Tests use an in-memory Durable Object storage fake, mocked Notify fetches, and a controlled clock. The signing helper reads its secret only from `NOTIFY_WEBHOOK_SECRET` in the environment and prints the two headers. With two rotation secrets, it signs with the first. It does not send a request:

```sh
body=$(node -e 'console.log(JSON.stringify({incident:crypto.randomUUID()}))')
node alert-receiver/tools/sign.mjs "$body"
```

Send exactly those body bytes during an approved drill, without adding a newline. The helper intentionally prints a signature for the drill; the receiver never logs signatures.

## Deploy (needs Preetham's approval, not done)

These are future steps, not actions taken by this draft:

1. On Preetham's machine, generate a shared secret with `openssl rand -hex 32`. Set it with `wrangler secret put NOTIFY_WEBHOOK_SECRET` in both the receiver and gateway Worker projects. Keep it out of files and command arguments.
2. Create a Notify agent token in Preetham's own account with only `notifications:send`; set `NOTIFY_API_TOKEN` using `wrangler secret put NOTIFY_API_TOKEN` in the receiver project. Pick his iPhone device ID and set it using `wrangler secret put NOTIFY_DEVICE_ID`.
3. Have Netops choose the route/hostname (Netops owns DNS) and fill in that routing configuration. `workers_dev` is disabled; this draft has no route, domain, or account ID. With approval, run `wrangler deploy` from `alert-receiver/`; the v1 migration creates the SQLite-backed ledger class.
4. Have Netops deploy the agreed gateway HMAC change and set gateway `NOTIFY_WEBHOOK` to `https://<host>/alert`.
5. Run the approved canary drill with Netops and confirm the incident arrives on Preetham's iPhone. The helper can sign an inert UUID-only probe to test the receiver; that probe is not a substitute for the end-to-end gateway canary. Do not include any content or key name in either webhook.

## Not tested

- Real Workers runtime, Durable Object storage/concurrency, and alarms.
- Real Notify delivery, device targeting, and idempotency behaviour.
- The real gateway, its proposed HMAC change, and the end-to-end canary.

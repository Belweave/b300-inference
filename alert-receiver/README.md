# Draft safety alert receiver

A dependency-free Cloudflare Worker for incident-only alerts from the B300 inference gateway. It runs separately from the B300 node, so a node outage does not stop accepted alerts from being delivered. Nothing has been deployed.

## Gateway contract

POST `/alert` with `Content-Type: application/json` and exactly `{"incident":"<lowercase-v4-uuid>"}`. Sign the exact UTF-8 body bytes, including whitespace:

- `X-Safety-Timestamp`: Unix seconds, ASCII digits only.
- `X-Safety-Signature`: `v1=` followed by the lowercase hex HMAC-SHA256 of `v1:<timestamp>:<rawBody>`, keyed by `NOTIFY_WEBHOOK_SECRET`.

The replay window defaults to ±300 seconds, inclusive, set by `REPLAY_WINDOW_S`. The receiver accepts one secret or two separated by exactly one space. To rotate, install both on the receiver, switch the gateway to the new single secret, then remove the old receiver secret after the replay window and outstanding gateway retries.

WebCrypto verifies the HMAC before JSON parsing. Authentication failures return 401 `{"error":"unauthorized"}`. Missing, empty, or malformed shared-secret configuration returns 503 `{"error":"unavailable"}` on every route; GET `/health` returns 503 `{"error":"unavailable","drill_mode":<bool>}`.

Netops implemented signing and five delivery attempts with 1, 2, 4, and 8 second retry backoffs inside `waitUntil`, plus an `alert_gave_up` log, in Belweave/infer-gateway PR #1 at `49aadcd`. Netops cross-checked the gateway's real sender against this receiver's `authenticated()`. Receiver retries only start after durable acceptance; they cannot recover a gateway request that never arrived.

## Behaviour

Bodies are limited to 256 bytes, checked against both Content-Length and the stream. Invalid JSON or incident fields return 400; oversized bodies return 413; non-JSON content types return 415. Only POST `/alert` is accepted (other methods: 405; unknown paths: 404). GET `/health` returns `{"ok":true,"drill_mode":false}` when configured, or `{"ok":true,"drill_mode":true}` in drill mode. `drill_mode` uses the same rule as sending: true only when `DRILL_MODE` is exactly `"1"`. The body has no other fields and never includes secrets or the webhook URL.

One `AlertLedger` Durable Object per incident stores pending work before trying Notify immediately. A new incident returns 202 with `sent` or `queued`; a duplicate returns 200 with its current `pending`, `sent`, or `failed` status without sending again. Ledger entries are retained for deduplication. Stored `first_seen` and `notified_at` timestamps are Unix milliseconds.

Each Notify attempt has a ten-second timeout and does not follow redirects. Failures retry by alarm after 30 seconds, 60 seconds, 2 minutes, 4 minutes, and so on, capped at 30 minutes per delay. `MAX_ATTEMPTS` defaults to 12, including the immediate attempt. All non-2xx responses retry. Responses 400, 404, and 409, or missing/invalid webhook configuration inside the ledger, log `notify_config_error`; other failures log `notify_failed`. Exhaustion persists `failed` and logs `notify_gave_up` at error level. Fixing configuration alone does not restart a failed incident.

The ledger serializes POSTs and alarms. It saves attempt state and a recovery alarm together before contacting Notify. Notify's webhook API honours `Idempotency-Key: b300-safety-<incident>`. Every attempt for an incident sends identical payload bytes, including after a restart. Any 2xx, including 202, marks the incident sent and logs `notify_sent`. A string `eventId` is stored privately as `notification_id`.

The outbound JSON has exactly four fields: `title` ("Child-safety block fired"), `summary` (incident ID and a CyberTipline reminder), `body` (the fixed reporting instructions), and `project` ("B300 safety"). The body includes the incident ID, automatic key revocation, sealed evidence in `b300-evidence` under `incidents/<id>.json`, and reporting to NCMEC's CyberTipline at https://report.cybertip.org/ under 18 U.S.C. 2258A. It contains no request content or key name. Without `deviceIds`, Notify fans out to the account's active devices; Preetham's iPhone is currently the only one.

Logs contain only structured event metadata and validated incident IDs, never credentials, signatures, request bodies, exception text, or Notify response bodies.

## Drill mode

Drill mode is on only when `DRILL_MODE` is exactly the string `"1"`. Unset and all other values, including `"0"`, `"true"`, `" 1"`, and the number `1`, use production behavior. Netops sets `DRILL_MODE=1` only for the supervised drill and must remove it before any production deploy.

The ledger decides the mode once when it first accepts an incident and stores `drill: true` only for drill incidents. Production records have no `drill` field. Every attempt uses that stored flag, so retries send identical payload bytes and headers after a restart or a change to `DRILL_MODE`.

Drill notifications prefix `title`, `summary`, and `body` with exactly `"DRILL, not a real incident. "`, including the trailing space, followed by the existing production text. The summary is prefixed because it can be what shows on the lock screen. `project` and the JSON field set and order stay unchanged. The Idempotency-Key is `b300-safety-drill-<incident>` instead of the production key `b300-safety-<incident>`. At acceptance, the receiver logs `{"event":"drill_mode","incident":"<incident>","drill":true}` exactly once with `console.log`, without the webhook URL or token. Retries and duplicate POSTs add no drill log line, and duplicates send nothing. Production logs are unchanged. Netops can check the live mode with GET `/health`: confirm `"drill_mode":true` before the drill and `"drill_mode":false` before any production deploy.

## Notify webhook

`NOTIFY_ALERT_WEBHOOK_URL` is the Notify webhook service URL for service "B300 CSAM alert". The receiver posts JSON to `/hooks/:token` with only `Content-Type` and `Idempotency-Key` headers. The URL token is the credential; there is no Authorization header.

Treat the URL as a secret: it is never logged, returned, or stored in ledger records. If it leaks, rotate it with Notify's service rotate, then update the receiver secret. The URL must parse, use HTTPS, and contain no username or password. Missing, empty, or invalid configuration returns 503 `{"error":"unavailable"}` on every route (GET `/health` adds only `drill_mode`) before touching the ledger and logs `{"event":"rejected","reason":"notify_unconfigured"}` at error level. If configuration disappears after acceptance, pending work remains on the alarm retry path.

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

Send exactly those body bytes during an approved drill, without adding a newline. The helper prints a signature for the drill; the receiver never logs signatures.

## Deploy (needs approval, not done)

The planned route is a Worker custom domain, `safety-alert.belweave.tech`, on the `belweave.tech` zone. `workers_dev` is off; the route is only a comment in `wrangler.toml`.

After approval:

1. Netops generates `NOTIFY_WEBHOOK_SECRET` with `openssl rand -hex 32` and sets it on both Workers. Netops also sets `NOTIFY_ALERT_WEBHOOK_URL` on the receiver. Supply both values through stdin to `wrangler secret put`, never in files, chat, or command arguments.
2. Netops activates the planned custom domain and sets gateway `NOTIFY_WEBHOOK=https://safety-alert.belweave.tech/alert`.
3. Before any production deploy, confirm `DRILL_MODE` is unset: `wrangler.toml` has no active `DRILL_MODE` var and there is no dashboard var.
4. Run `wrangler deploy` from `alert-receiver/`. The v1 migration creates the SQLite-backed ledger class.
5. Run the supervised gateway drill with Netops and confirm delivery to Preetham's iPhone. The helper can sign an inert UUID-only receiver probe, but that probe does not replace the end-to-end gateway drill. Include no content or key name in either webhook.

## Not tested

- Real Workers runtime, Durable Object storage/concurrency, and alarms.
- Real Notify webhook delivery and its idempotency.
- The end-to-end gateway drill.

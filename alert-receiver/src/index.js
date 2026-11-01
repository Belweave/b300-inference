const encoder = new TextEncoder();
const INCIDENT = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const BODY_LIMIT = 256;
const TIMEOUT_MS = 10_000;
const validIncident = (value) => typeof value === "string" && value.length === 36 && INCIDENT.test(value);

const now = (env) => (env.NOW ? env.NOW() : Date.now());
const json = (body, status = 200, headers = {}) => Response.json(body, { status, headers });

function rejected(status, error, reason = error) {
  console.error(JSON.stringify({ event: "rejected", reason }));
  return json({ error }, status);
}

function integer(value, fallback, minimum) {
  const parsed = Number(value);
  return /^\d+$/.test(String(value)) && Number.isSafeInteger(parsed) && parsed >= minimum
    ? parsed : fallback;
}

function secrets(value) {
  if (typeof value !== "string") return null;
  const parts = value.split(" ");
  return parts.length <= 2 && parts.every((part) => part.length > 0 && !/\s/.test(part)) ? parts : null;
}

async function readBody(request) {
  const bytes = new Uint8Array(BODY_LIMIT);
  let length = 0;
  if (!request.body) return bytes.subarray(0, 0);
  const reader = request.body.getReader();
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return bytes.subarray(0, length);
      if (length + value.byteLength > BODY_LIMIT) {
        void reader.cancel().catch(() => {});
        return null;
      }
      bytes.set(value, length);
      length += value.byteLength;
    }
  } finally {
    reader.releaseLock();
  }
}

async function authenticated(request, env, raw, keys) {
  const timestamp = request.headers.get("X-Safety-Timestamp");
  const signature = request.headers.get("X-Safety-Signature");
  if (!timestamp || !/^[0-9]+$/.test(timestamp) || !/^v1=[0-9a-f]{64}$/.test(signature ?? "")) return false;
  const seconds = Number(timestamp);
  if (!Number.isSafeInteger(seconds) ||
      Math.abs(now(env) / 1000 - seconds) > integer(env.REPLAY_WINDOW_S, 300, 0)) return false;

  const prefix = encoder.encode(`v1:${timestamp}:`);
  const message = new Uint8Array(prefix.length + raw.length);
  message.set(prefix);
  message.set(raw, prefix.length);
  const digest = Uint8Array.from(signature.slice(3).match(/../g), (hex) => parseInt(hex, 16));
  const matches = await Promise.all(keys.map(async (secret) => {
    const key = await crypto.subtle.importKey("raw", encoder.encode(secret),
      { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
    return crypto.subtle.verify("HMAC", key, digest, message);
  }));
  return matches.some(Boolean);
}

export default {
  async fetch(request, env) {
    const keys = secrets(env.NOTIFY_WEBHOOK_SECRET);
    if (!keys) return rejected(503, "unavailable");
    const path = new URL(request.url).pathname;
    if (path === "/health" && request.method === "GET") return json({ ok: true });
    if (path !== "/alert") return rejected(404, "not_found");
    if (request.method !== "POST") {
      return json({ error: "method_not_allowed" }, 405, { Allow: "POST" });
    }
    const contentLength = request.headers.get("Content-Length");
    if (contentLength !== null && !/^[0-9]+$/.test(contentLength)) return rejected(400, "bad_request");
    if (contentLength !== null && Number(contentLength) > BODY_LIMIT) {
      if (request.body) void request.body.cancel().catch(() => {});
      return rejected(413, "payload_too_large");
    }
    if (request.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
      return rejected(415, "unsupported_media_type");
    }
    let raw;
    try {
      raw = await readBody(request);
    } catch {
      return rejected(400, "bad_request");
    }
    if (raw === null) return rejected(413, "payload_too_large");
    if (!await authenticated(request, env, raw, keys)) return rejected(401, "unauthorized", "auth");

    let data;
    try {
      data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
    } catch {
      return rejected(400, "bad_request");
    }
    if (!data || Array.isArray(data) || typeof data !== "object" ||
        Object.keys(data).length !== 1 || !validIncident(data.incident)) {
      return rejected(400, "bad_request");
    }
    const id = env.LEDGER.idFromName(data.incident);
    return env.LEDGER.get(id).fetch(new Request("https://ledger.invalid/alert", {
      method: "POST", body: data.incident,
    }));
  },
};

function notification(incident, device) {
  return {
    title: "Child-safety block fired",
    summary: `Incident ${incident}. File a CyberTipline report.`,
    body: `A child-safety hard-block fired on the B300 inference gateway. Incident ID: ${incident}. ` +
      `The offending key was revoked automatically and sealed evidence is in the b300-evidence bucket under incidents/${incident}.json. ` +
      "Report to NCMEC's CyberTipline (https://report.cybertip.org/) as required by 18 U.S.C. 2258A. " +
      "Do not open or share the evidence outside the reporting process.",
    project: "B300 safety",
    ...(device ? { deviceIds: [device] } : {}),
  };
}

export class AlertLedger {
  constructor(ctx, env) {
    this.storage = ctx.storage;
    this.env = env;
    this.queue = Promise.resolve();
  }

  // Keep fetches and alarms serialized even while Notify is in flight.
  exclusive(work) {
    const result = this.queue.then(work);
    this.queue = result.catch(() => {});
    return result;
  }

  async save(record, alarmAt = null) {
    await this.storage.transaction(async (txn) => {
      await txn.put("record", record);
      if (alarmAt === null) await txn.deleteAlarm();
      else await txn.setAlarm(alarmAt);
    });
  }

  fetch(request) {
    return this.exclusive(async () => {
      const incident = await request.text();
      if (!validIncident(incident)) return rejected(400, "bad_request");
      let record = await this.storage.get("record");
      if (record) return json({ incident, status: record.status });
      record = { incident, status: "pending", attempts: 0, first_seen: now(this.env) };
      record.retry_at = now(this.env) + 30_000;
      await this.save(record, record.retry_at);
      await this.attempt(record);
      return json({ incident, status: record.status === "sent" ? "sent" : "queued" }, 202);
    });
  }

  alarm() {
    return this.exclusive(async () => {
      const record = await this.storage.get("record");
      if (!record || record.status !== "pending") {
        await this.storage.deleteAlarm();
        return;
      }
      if (record.retry_at > now(this.env)) {
        await this.storage.setAlarm(record.retry_at);
        return;
      }
      await this.attempt(record);
    });
  }

  async giveUp(record) {
    record.status = "failed";
    delete record.retry_at;
    await this.save(record);
    console.error(JSON.stringify({ event: "notify_gave_up", incident: record.incident, attempt: record.attempts, status: "failed" }));
  }

  async attempt(record) {
    const maximum = integer(this.env.MAX_ATTEMPTS, 12, 1);
    if (record.attempts >= maximum) return this.giveUp(record);
    record.attempts += 1;
    const delay = Math.min(30 * 2 ** (record.attempts - 1), 1800) * 1000;
    // Persist a recovery alarm before I/O in case the object stops mid-attempt.
    record.retry_at = now(this.env) + TIMEOUT_MS + delay;
    await this.save(record, record.retry_at);
    const result = await this.deliver(record.incident);
    const log = { incident: record.incident, attempt: record.attempts, status: result.status };
    if (result.ok) {
      record.status = "sent";
      record.notified_at = now(this.env);
      if (result.id !== undefined) record.notification_id = result.id;
      delete record.retry_at;
      await this.save(record);
      console.log(JSON.stringify({ event: "notify_sent", ...log }));
    } else {
      const configError = [400, 401, 403, 404, "config"].includes(result.status);
      console.error(JSON.stringify({ event: configError ? "notify_config_error" : "notify_failed", ...log }));
      if (record.attempts >= maximum) return this.giveUp(record);
      record.retry_at = now(this.env) + delay;
      await this.save(record, record.retry_at);
    }
  }

  async deliver(incident) {
    if (!this.env.NOTIFY_API_TOKEN) return { ok: false, status: "config" };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const origin = (this.env.NOTIFY_API_ORIGIN || "https://notify.belweave.ai").replace(/\/$/, "");
      const response = await fetch(`${origin}/api/agent/notifications`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.env.NOTIFY_API_TOKEN}`,
          "Idempotency-Key": `b300-safety-${incident}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(notification(incident, this.env.NOTIFY_DEVICE_ID)),
        signal: controller.signal,
        redirect: "manual",
      });
      if (!response.ok) {
        if (response.body) void response.body.cancel().catch(() => {});
        return { ok: false, status: response.status };
      }
      const data = await response.json().catch(() => null);
      const id = data?.id;
      return { ok: true, status: response.status, ...(typeof id === "string" ? { id } : {}) };
    } catch {
      return { ok: false, status: controller.signal.aborted ? "timeout" : "network" };
    } finally {
      clearTimeout(timer);
    }
  }
}

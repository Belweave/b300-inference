const secretConfig = process.env.NOTIFY_WEBHOOK_SECRET;
const body = process.argv[2];
const secrets = secretConfig?.split(" ");
if (process.argv.length !== 3 || body === undefined || !secrets || secrets.length > 2 ||
    secrets.some((secret) => !secret || /\s/.test(secret))) {
  console.error("Usage: NOTIFY_WEBHOOK_SECRET=<env value> node alert-receiver/tools/sign.mjs '<body>'");
  process.exitCode = 1;
} else {
  const encoder = new TextEncoder();
  const timestamp = String(Math.floor(Date.now() / 1000));
  const key = await crypto.subtle.importKey("raw", encoder.encode(secrets[0]),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, encoder.encode(`v1:${timestamp}:${body}`));
  const hex = Array.from(new Uint8Array(mac), (byte) => byte.toString(16).padStart(2, "0")).join("");
  console.log(`X-Safety-Timestamp: ${timestamp}\nX-Safety-Signature: v1=${hex}`);
}

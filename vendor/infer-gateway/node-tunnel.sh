#!/usr/bin/env bash
# Run on each fresh B300 node after the model server is healthy on 127.0.0.1:8000.
# Same named tunnel every window, so a new node joins as a replica while the old
# one is still connected (zero-downtime handoff).
set -euo pipefail
: "${CF_TUNNEL_TOKEN:?set CF_TUNNEL_TOKEN}"

CFD_VERSION="2026.10.7"
case "$(uname -m)" in
  x86_64)  ARCH=amd64; SHA=1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872 ;;
  aarch64|arm64) ARCH=arm64; SHA=3df43aedc9ff291d1735b5cb576cb97e0b157488796db2e32b28aecabb445775 ;;
  *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac

BIN=/tmp/cloudflared
curl -fsSL -o "$BIN" \
  "https://github.com/cloudflare/cloudflared/releases/download/${CFD_VERSION}/cloudflared-linux-${ARCH}"
echo "${SHA}  ${BIN}" | sha256sum -c - >/dev/null || { echo "cloudflared checksum mismatch" >&2; rm -f "$BIN"; exit 1; }
chmod +x "$BIN"

# cloudflared reads TUNNEL_TOKEN from the environment; keep it off argv (ps, /proc/*/cmdline).
TUNNEL_TOKEN="$CF_TUNNEL_TOKEN" nohup "$BIN" tunnel --no-autoupdate run \
  --url http://127.0.0.1:8000 >/tmp/cloudflared.log 2>&1 &
echo "tunnel started, pid $!"

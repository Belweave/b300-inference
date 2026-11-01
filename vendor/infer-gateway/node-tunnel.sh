#!/usr/bin/env bash
# Run on each fresh B300 node after the model server is up on localhost:8000.
# TUNNEL_TOKEN is the same named tunnel every window, so a new node joins as a
# replica while the old one is still connected: zero-downtime handoff.
set -euo pipefail
: "${TUNNEL_TOKEN:?set TUNNEL_TOKEN}"
curl -fsSL -o /tmp/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x /tmp/cloudflared
nohup /tmp/cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN" --url http://localhost:8000 >/tmp/cloudflared.log 2>&1 &
echo "tunnel started, pid $!"

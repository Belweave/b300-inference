#!/usr/bin/env bash
set -euo pipefail
# Optional node -> relay SSH transport only. Relay cloudflared is owned by Netops.
if [[ ${DRY_RUN:-0} == 1 ]]; then
    echo 'DRY RUN: optional ssh -N -R 127.0.0.1:RELAY_PORT:127.0.0.1:8000 RELAY_TARGET; keepalive/reconnect until T-10m'
    exit 0
fi
: "${REVERSE_RELAY_TARGET:?existing user@relay required}"
: "${REVERSE_RELAY_PORT:?relay-local listener port required}"
: "${WINDOW_END:?expiry required}"
[[ $REVERSE_RELAY_TARGET =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]] || exit 1
[[ $REVERSE_RELAY_PORT =~ ^[0-9]+$ ]] && (( REVERSE_RELAY_PORT >= 1024 && REVERSE_RELAY_PORT <= 65535 )) || exit 1
opts=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes)
if [[ -n ${REVERSE_SSH_KEY_PATH:-} ]]; then opts+=(-i "$REVERSE_SSH_KEY_PATH"); fi
child=''
cleanup() { if [[ -n $child ]]; then kill "$child" 2>/dev/null || true; fi; }
trap cleanup EXIT
trap 'exit 0' TERM INT
while (( $(date +%s) < WINDOW_END-600 )) && [[ ! -e ${RUN_DIR:?}/stop ]]; do
    ssh -N "${opts[@]}" -R "127.0.0.1:$REVERSE_RELAY_PORT:127.0.0.1:8000" "$REVERSE_RELAY_TARGET" &
    child=$!
    # Check expiry even while the tunnel is healthy.
    while kill -0 "$child" 2>/dev/null && (( $(date +%s) < WINDOW_END-600 )) && [[ ! -e $RUN_DIR/stop ]]; do sleep 2; done
    cleanup
    wait "$child" || true
    child=''
    sleep 3
done

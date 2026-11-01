#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/node-env.sh
source "$ROOT/scripts/node-env.sh"
# shellcheck source=scripts/common.sh
source "$ROOT/scripts/common.sh"
if [[ $DRY_RUN == 1 ]]; then echo 'DRY RUN: collect; bounded drain; stop engine and connector; archive; optional upload'; exit 0; fi
umask 077
mkdir -p "$RUN_DIR"
# Restore only nonsensitive metadata. Secrets must remain in the caller environment.
if [[ -f $RUN_DIR/lifecycle.json ]]; then
    while IFS='=' read -r name value; do export "$name=$value"; done < <(python3 - "$RUN_DIR/lifecycle.json" <<'PY'
import json,sys
for k,v in json.load(open(sys.argv[1])).items(): print(k+'='+str(v))
PY
    )
fi
export ENGINE_PORT=$PORT ENDPOINT="http://127.0.0.1:$PORT"
need flock
exec 9>"$KIT_ROOT/runs/node.lock"
flock -w 30 9 || { echo 'Lifecycle lock busy; retry teardown' >&2; exit 1; }
[[ ! -e $RUN_DIR/done ]] || { log 'Already torn down'; exit 0; }
touch "$RUN_DIR/stop"
# Stop a benchmark owned by this run before snapshotting its files.
if [[ -f $RUN_DIR/bench.pid ]]; then
    bench_pid=$(cat "$RUN_DIR/bench.pid")
    if [[ $bench_pid =~ ^[0-9]+$ && -r /proc/$bench_pid/cmdline ]] && tr '\0' ' ' < "/proc/$bench_pid/cmdline" | grep -Fq "$KIT_ROOT/loadtest.sh"; then
        kill -- "-$bench_pid" 2>/dev/null || true
    fi
fi
status collecting running
nvidia-smi -q 2>&1 | python3 "$KIT_ROOT/scripts/redact.py" > "$RUN_DIR/nvidia-final.txt" || true
python3 "$KIT_ROOT/scripts/origin_request.py" /metrics 2>/dev/null | python3 "$KIT_ROOT/scripts/redact.py" > "$RUN_DIR/metrics.txt" || true
stop_connector() {
    if [[ -f $RUN_DIR/cloudflared.pid ]]; then
        local pid
        pid=$(cat "$RUN_DIR/cloudflared.pid")
        if [[ $pid =~ ^[0-9]+$ && -r /proc/$pid/comm && $(cat "/proc/$pid/comm") == cloudflared ]]; then
            kill "$pid" 2>/dev/null || true
        else
            # Dedicated one-node kit only; exact executable prefix, never generic cloudflared.
            pkill -f '^/tmp/cloudflared tunnel ' 2>/dev/null || true
        fi
    fi
}
# Always disconnect on unexpected failure, including failure to stop Docker.
trap stop_connector EXIT
status draining running
if [[ ${REPLACEMENT_READY:-0} == 1 ]]; then stop_connector; fi
if [[ -n ${DRAIN_HOOK:-} ]]; then
    # Operator-supplied executable must reject new admission without killing active work.
    timeout 10 "$DRAIN_HOOK" || log 'Drain hook failed; continuing bounded drain'
fi
if docker inspect "$SERVER_NAME" >/dev/null 2>&1; then python3 "$KIT_ROOT/scripts/drain.py"; fi
stop_service() {
    local name=$1
    if [[ $(docker inspect -f '{{index .Config.Labels "b300.run"}}' "$name" 2>/dev/null || true) == "$RUN_ID" ]]; then
        docker stop -t 10 "$name" >/dev/null || true
        docker rm -f "$name" >/dev/null || true
    fi
}
stop_service "$GATEWAY_NAME"
stop_service "$SERVER_NAME"
stop_connector
if [[ -f $RUN_DIR/cloudflared.pid ]]; then
    python3 "$KIT_ROOT/scripts/tunnel_log.py" collect /tmp/cloudflared.log \
        "$RUN_DIR/cloudflared-log-cursor.json" "$RUN_DIR/cloudflared.log"
fi
if [[ -f $RUN_DIR/reverse.pid ]]; then
    reverse_pid=$(cat "$RUN_DIR/reverse.pid")
    if [[ $reverse_pid =~ ^[0-9]+$ && -r /proc/$reverse_pid/cmdline ]] && tr '\0' ' ' < "/proc/$reverse_pid/cmdline" | grep -Fq "$KIT_ROOT/scripts/reverse-tunnel.sh"; then
        kill "$reverse_pid" 2>/dev/null || true
    fi
fi
status stopped ok
# Log writers have closed after container shutdown. Never archive env files or weights.
archive="$KIT_ROOT/runs/$RUN_ID.tar.gz"
# Pushed image archives can be tens of GB; never package them as diagnostics.
python3 "$KIT_ROOT/scripts/archive.py" "$RUN_DIR" "$archive"
if [[ -n ${ARTIFACT_S3_URI:-} ]]; then
    upload_cmd=(s5cmd)
    if [[ -n ${S3_ENDPOINT_URL:-} ]]; then upload_cmd+=(--endpoint-url "$S3_ENDPOINT_URL"); fi
    if timeout 90 "${upload_cmd[@]}" cp "$archive" "${ARTIFACT_S3_URI%/}/$RUN_ID.tar.gz" >/dev/null 2>&1; then
        log 'Artifact uploaded'
    else
        log 'Artifact upload failed; local archive retained'
    fi
fi
touch "$RUN_DIR/done"
if [[ -f $KIT_ROOT/runs/active && $(cat "$KIT_ROOT/runs/active") == "$RUN_ID" ]]; then rm "$KIT_ROOT/runs/active"; fi
log "Teardown complete: $archive (rental release remains a provider hook)"

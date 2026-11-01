#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
if [[ ${1:-} == --claim ]]; then exec "$ROOT/watcher/deploy.sh" "${2:?claim JSON path required}"; fi
# shellcheck source=scripts/common.sh
source "$ROOT/scripts/common.sh"
case "$ENGINE" in sglang|vllm) ;; *) die 'ENGINE must be sglang or vllm';; esac
set -a
# shellcheck source=/dev/null
source "$KIT_ROOT/config/$ENGINE.env"
set +a
[[ $PARALLELISM == tp || $PARALLELISM == dp_ep ]] || die 'Invalid PARALLELISM'
[[ $SPECULATION == none || $SPECULATION == dspark ]] || die 'Invalid SPECULATION'
if [[ $DRY_RUN == 1 ]]; then
    cat <<PLAN
DRY RUN: no GPU, Docker, provider, SSH, downloads, or tunnel operations.
1. Preflight: Linux x86_64; 8x B300; driver >=580; CUDA >=13; NVLink; disk.
2. Check Docker + NVIDIA toolkit; arm node-local teardown at window end minus 10m.
3. In parallel: pull $ENGINE_IMAGE and verify/resume $WEIGHTS_SOURCE weights.
4. Launch $ENGINE: TP8, requests=$MAX_RUNNING_REQUESTS, prefill=$CHUNKED_PREFILL_SIZE,
   graph max=$CUDA_GRAPH_MAX_BS, context=$CONTEXT_LENGTH, speculation=$SPECULATION; native KV format.
5. Bind 127.0.0.1:$PORT; api-key from INFER_UPSTREAM_KEY (ORIGIN_KEY alias).
6. Wait for /health and authenticated /v1/chat/completions smoke.
7. Optional gateway=$GATEWAY; tunnel=$TUNNEL via verbatim Netops script after smoke.
   Public infer.belweave.tech -> infer-origin.belweave.tech -> localhost:8000.
8. Record status/digests; teardown collects, drains within budget, stops services.
PLAN
    exit 0
fi
umask 077
[[ $(uname -s) == Linux ]] || die 'Node bootstrap requires Linux'
[[ $PORT == 8000 ]] || die 'Netops contract requires PORT=8000'
[[ -n $INFER_UPSTREAM_KEY ]] || die 'INFER_UPSTREAM_KEY (or ORIGIN_KEY) required'
case "$TUNNEL" in cloudflare) [[ $(uname -m) == x86_64 ]] || die 'Vendored tunnel requires x86_64'; : "${CF_TUNNEL_TOKEN:?required}";; none) ;; tailscale) die 'Tailscale requires a separately reviewed hook; no automatic funnel';; *) die 'Invalid TUNNEL';; esac
case "$GATEWAY" in none) ;; litellm) : "${LITELLM_MASTER_KEY:?required}"; : "${LITELLM_IMAGE:?pin a LiteLLM image digest}";; *) die 'Invalid GATEWAY';; esac
need python3; need flock; need setsid; need docker; need nvidia-smi; need curl
mkdir -p "$RUN_DIR"
exec 9>"$KIT_ROOT/runs/node.lock"
flock -n 9 || die 'Another bootstrap/teardown is active'
[[ ! -e $RUN_DIR/stop && ! -e $RUN_DIR/done ]] || die 'Run is stopped; use a new RUN_ID'
if [[ -f $KIT_ROOT/runs/active && $(cat "$KIT_ROOT/runs/active") != "$RUN_ID" ]]; then die 'Another run owns this node; tear it down first'; fi
printf '%s\n' "$RUN_ID" > "$KIT_ROOT/runs/active"
# Save only nonsecret lifecycle configuration for manual teardown.
if [[ -f $RUN_DIR/lifecycle.json && -z ${WINDOW_END:-} ]]; then
    WINDOW_END=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["WINDOW_END"])' "$RUN_DIR/lifecycle.json")
fi
export WINDOW_END=${WINDOW_END:-$(($(date +%s) + ${WINDOW_MINUTES:-240} * 60))}
[[ $WINDOW_END =~ ^[0-9]+$ ]] || die 'WINDOW_END must be epoch seconds'
(( WINDOW_END > $(date +%s) + 660 )) || die 'Less than 11 minutes remain'
python3 - <<'PY'
import json,os
from pathlib import Path
p=Path(os.environ['RUN_DIR'])/'lifecycle.json'
if p.exists():
 old=json.loads(p.read_text())
 if int(old['WINDOW_END']) != int(os.environ['WINDOW_END']):
  raise SystemExit('Reruns require the original WINDOW_END; do not extend the deadline')
else:
 p.write_text(json.dumps({k:os.environ[k] for k in ('RUN_ID','WINDOW_END','ENGINE','MODEL','MODEL_REVISION','PORT','TUNNEL','GATEWAY')}))
PY
jobs_to_stop=()
failed() {
    trap - ERR TERM INT
    set +e
    status "${PHASE:-bootstrap}" error
    local pid
    for pid in "${jobs_to_stop[@]}"; do kill -- "-$pid" 2>/dev/null; done
    touch "$RUN_DIR/stop"
    flock -u 9
    "$KIT_ROOT/teardown.sh"
    exit 1
}
trap failed ERR TERM INT
phase() { PHASE=$1; log "$PHASE"; status "$PHASE" running; }
python3 - "$$" <<'PYOWNER'
import json,os,sys
from pathlib import Path
pid=int(sys.argv[1])
Path(os.environ['RUN_DIR'],'bootstrap-owner.json').write_text(json.dumps({'pid':pid,'start':Path(f'/proc/{pid}/stat').read_text().split()[21]}))
PYOWNER
phase preflight
python3 - <<'PY'
import csv,json,os,re,shutil,subprocess
from pathlib import Path
r=Path(os.environ['RUN_DIR'])
rows=list(csv.reader(subprocess.check_output(['nvidia-smi','--query-gpu=name,uuid,memory.total,driver_version','--format=csv,noheader,nounits'],text=True).splitlines()))
assert len(rows)==8 and all('B300' in x[0] for x in rows), 'Need exactly eight B300 GPUs'
assert all(int(x[3].strip().split('.')[0])>=580 for x in rows), 'Need driver >=580 for CUDA 13'
summary=subprocess.check_output(['nvidia-smi'],text=True)
m=re.search(r'CUDA Version:\s*(\d+\.\d+)',summary)
assert m and float(m[1])>=13, 'Driver CUDA capability must be >=13'
(r/'gpu.json').write_text(json.dumps(rows))
(r/'nvidia-smi.txt').write_text(summary)
topo=subprocess.check_output(['nvidia-smi','topo','-m'],text=True)
(r/'nvlink.txt').write_text(topo)
assert len(re.findall(r'NV\d+',topo))>=56, 'Expected all eight GPUs connected by NVLink'
p=Path(os.environ['WEIGHTS_DIR']); p.mkdir(parents=True,exist_ok=True)
needed=int(os.environ.get('MIN_FREE_GB','650'))*10**9
# Existing files are reusable, but checksums are still mandatory.
existing=sum(x.stat().st_size for x in p.rglob('*.safetensors'))
assert shutil.disk_usage(p).free >= max(20*10**9,needed-existing), 'Insufficient weight disk space'
PY
phase runtime
need nvidia-container-cli
nvidia-container-cli --version > "$RUN_DIR/toolkit.txt"
docker info >/dev/null
# Timer is armed before any large I/O. Its environment stays in memory.
if [[ ! -f $RUN_DIR/timer.pid ]] || ! kill -0 "$(cat "$RUN_DIR/timer.pid")" 2>/dev/null; then
    nohup python3 "$KIT_ROOT/scripts/deadline.py" "$$" </dev/null > /dev/null 2>&1 9>&- &
    echo "$!" > "$RUN_DIR/timer.pid"
fi
phase pull
export HF_XET_HIGH_PERFORMANCE=${HF_XET_HIGH_PERFORMANCE:-1}
# Expand exported variables inside the isolated child process group.
# shellcheck disable=SC2016
setsid bash -c 'set -euo pipefail; docker pull "$ENGINE_IMAGE" 2>&1 | python3 "$KIT_ROOT/scripts/redact.py" > "$RUN_DIR/pull.log"' 9>&- &
pull_pid=$!; jobs_to_stop+=("$pull_pid")
# Expand exported variables inside the isolated child process group.
# shellcheck disable=SC2016
setsid bash -c 'set -euo pipefail; python3 "$KIT_ROOT/scripts/weights.py" 2>&1 | python3 "$KIT_ROOT/scripts/redact.py" > "$RUN_DIR/weights.log"' 9>&- &
weights_pid=$!; jobs_to_stop+=("$weights_pid")
wait "$pull_pid"; wait "$weights_pid"
jobs_to_stop=()
docker image inspect "$ENGINE_IMAGE" --format '{{json .RepoDigests}}' > "$RUN_DIR/image-digests.json"
# Runtime hardware probe is cheap and catches incompatible PyTorch wheels.
docker run --rm --gpus all --entrypoint python3 "$ENGINE_IMAGE" -c 'import torch; assert torch.cuda.device_count()==8; assert torch.cuda.get_device_capability()==(10,3); print(torch.__version__,torch.version.cuda,torch.cuda.get_arch_list())' > "$RUN_DIR/torch.txt"
python3 - <<'PYMETA'
import json,os,subprocess
from pathlib import Path
p=Path(os.environ['RUN_DIR'])
p.joinpath('runtime.json').write_text(json.dumps({'image':os.environ['ENGINE_IMAGE'],
 'digests':json.loads(p.joinpath('image-digests.json').read_text()),
 'argv_without_key':subprocess.check_output(['python3',os.environ['KIT_ROOT']+'/scripts/engine.py']).decode().strip('\0').split('\0')}))
PYMETA
phase launch
[[ ! -e $RUN_DIR/stop ]] || die 'Deadline reached'
if ! docker inspect "$SERVER_NAME" >/dev/null 2>&1; then
    docker create -i --name "$SERVER_NAME" --label "b300.run=$RUN_ID" --gpus all --network host --ipc host \
        --log-driver none --mount "type=bind,src=$WEIGHTS_DIR,dst=/weights,readonly" \
        --mount "type=bind,src=$KIT_ROOT,dst=/kit,readonly" --entrypoint python3 "$ENGINE_IMAGE" /kit/scripts/serve.py >/dev/null
fi
if [[ $(docker inspect -f '{{.State.Running}}' "$SERVER_NAME") != true ]]; then
    ( python3 "$KIT_ROOT/scripts/payload.py" engine | docker start -ai "$SERVER_NAME" 2>&1 | python3 "$KIT_ROOT/scripts/redact.py" >> "$RUN_DIR/engine.log" ) 9>&- &
fi
phase health
health_end=$(($(date +%s) + ${HEALTH_TIMEOUT_SECONDS:-2400}))
while ! python3 "$KIT_ROOT/scripts/origin_request.py" /health 2>/dev/null >/dev/null; do
    (( $(date +%s) < health_end && $(date +%s) < WINDOW_END-600 )) || die 'Health deadline exceeded'
    container_state=$(docker inspect -f '{{.State.Status}}' "$SERVER_NAME")
    [[ $container_state != exited && $container_state != dead ]] || die 'Engine exited; inspect engine.log'
    sleep 5
done
python3 "$KIT_ROOT/scripts/smoke.py"
phase gateway
if [[ $GATEWAY == litellm ]]; then
    export LITELLM_UPSTREAM_MODEL="openai/$MODEL" LITELLM_UPSTREAM_URL="$ENDPOINT/v1"
    if ! docker inspect "$GATEWAY_NAME" >/dev/null 2>&1; then
        docker pull "$LITELLM_IMAGE" >/dev/null
        docker create -i --name "$GATEWAY_NAME" --label "b300.run=$RUN_ID" --network host --log-driver none \
            --mount "type=bind,src=$KIT_ROOT,dst=/kit,readonly" --entrypoint python3 "$LITELLM_IMAGE" /kit/scripts/serve.py >/dev/null
    fi
    if [[ $(docker inspect -f '{{.State.Running}}' "$GATEWAY_NAME") != true ]]; then
        ( python3 "$KIT_ROOT/scripts/payload.py" gateway | docker start -ai "$GATEWAY_NAME" 2>&1 | python3 "$KIT_ROOT/scripts/redact.py" >> "$RUN_DIR/gateway.log" ) 9>&- &
    fi
    for _ in {1..60}; do
        if SMOKE_GATEWAY=1 SMOKE_ENDPOINT="http://127.0.0.1:$GATEWAY_PORT" python3 "$KIT_ROOT/scripts/smoke.py" 2>/dev/null; then break; fi
        sleep 2
    done
    SMOKE_GATEWAY=1 SMOKE_ENDPOINT="http://127.0.0.1:$GATEWAY_PORT" python3 "$KIT_ROOT/scripts/smoke.py"
fi
phase tunnel
if [[ $TUNNEL == cloudflare ]]; then
    if [[ ! -f $RUN_DIR/cloudflared.pid ]] || ! kill -0 "$(cat "$RUN_DIR/cloudflared.pid")" 2>/dev/null; then
        tunnel_output=$(TUNNEL_TOKEN="$CF_TUNNEL_TOKEN" "$KIT_ROOT/vendor/infer-gateway/node-tunnel.sh" 9>&-)
        tunnel_pid=$(printf '%s\n' "$tunnel_output" | sed -n 's/^tunnel started, pid \([0-9][0-9]*\)$/\1/p')
        [[ $tunnel_pid =~ ^[0-9]+$ ]] || die 'Tunnel helper returned no PID'
        printf '%s\n' "$tunnel_pid" > "$RUN_DIR/cloudflared.pid"
        sleep 2
        kill -0 "$tunnel_pid" || die 'Tunnel exited'
    fi
fi
status serving ok
log "Ready: $ENDPOINT; status: $RUN_DIR/status.json"

#!/usr/bin/env bash
# Remote commands intentionally expand validated local paths and run IDs.
# shellcheck disable=SC2029
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
claim=${1:?normalized claim JSON required}
if [[ ${DRY_RUN:-0} == 1 ]]; then echo 'DRY RUN: stable claim ID; probe node; push if offline; bootstrap; relay up after ready; down after drain; retrieve archive'; exit 0; fi
umask 077
values=()
while IFS= read -r value; do values+=("$value"); done < <(python3 "$ROOT/claim/validate.py" "$claim")
[[ ${#values[@]} == 6 ]] || { echo 'Claim validation failed' >&2; exit 1; }
host=${values[0]}; port=${values[1]}; user=${values[2]}; key=${values[3]}
export WINDOW_END=${values[4]} RUN_ID=${values[5]}
# shellcheck source=scripts/common.sh
source "$ROOT/scripts/common.sh"
remote=${NODE_KIT_DIR:-b300-inference}
[[ $remote =~ ^[a-zA-Z0-9_/-]+$ && $remote != /* && $remote != *..* ]] || die 'NODE_KIT_DIR must be a safe relative path'
ssh_args=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -p "$port" -i "$key")
target="$user@$host"
export NODE_SSH_PORT=$port NODE_SSH_KEY_PATH=$key
ssh "${ssh_args[@]}" "$target" "mkdir -p $remote"
# Copy the provisioned file opaquely. The Mac never sources or reads its contents.
export MAC_NODE_ENV_FILE=${MAC_NODE_ENV_FILE:-$HOME/.config/b300/node.env}
export NODE_ENV_FILE=${NODE_ENV_FILE:-node.env}
[[ $NODE_ENV_FILE =~ ^[a-zA-Z0-9_./-]+$ && $NODE_ENV_FILE != *..* ]] || die 'NODE_ENV_FILE must be a safe absolute or home-relative path'
node_env_destination=$NODE_ENV_FILE
if [[ $NODE_ENV_FILE != /* ]]; then
    # Remote SSH starts in home; node bootstrap later expands this relative path.
    # shellcheck disable=SC2088
    export NODE_ENV_FILE="~/$NODE_ENV_FILE"
fi
[[ -f $MAC_NODE_ENV_FILE ]] || die 'MAC_NODE_ENV_FILE is missing; provision node.env through Netops'
scp -p -P "$port" -i "$key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 \
    "$MAC_NODE_ENV_FILE" "$target:$node_env_destination"
ssh "${ssh_args[@]}" "$target" "chmod 600 $node_env_destination"
stage_exclude=()
if [[ $STAGING_DIR == "$ROOT/"* ]]; then stage_exclude+=(--exclude="${STAGING_DIR#"$ROOT/"}/"); fi
rsync -az ${stage_exclude[@]+"${stage_exclude[@]}"} -e "$ROOT/watcher/ssh-rsync.sh" \
    --include='/config/sglang.env' --include='/config/vllm.env' --include='/config/throughput.env' \
    --exclude='.git/' --exclude='.env*' --exclude='*.env' \
    --exclude='runs/' --exclude='logs/' --exclude='staging/' --exclude='__pycache__/' --exclude='.venv/' --exclude='*.fixture.txt' \
    "$ROOT/" "$target:$remote/"
mkdir -p "$RUN_DIR"
python3 "$ROOT/watcher/transport.py" | ssh "${ssh_args[@]}" "$target" "cd $remote && python3 watcher/node-network.py" > "$RUN_DIR/network.json"
selected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["selected"])' "$RUN_DIR/network.json")
export NETWORK_PREPROBED=1
if [[ $selected == ssh ]]; then
    export IMAGE_SOURCE=push
    if [[ $WEIGHTS_SOURCE != volume ]]; then export WEIGHTS_SOURCE=push; fi
    if [[ $SSH_DIRECTION == forward ]]; then
        : "${RELAY_SH:?SSH forward mode requires external Netops relay.sh}"
        if [[ -n ${RELAY_HOST:-} ]]; then : "${RELAY_NODE_SSH_KEY_PATH:?key path on remote relay required}"; fi
    else
        "$ROOT/watcher/notify.sh" milestone 'Reverse SSH selected: node transport only; Netops must provision the relay-side connector separately'
    fi
fi
if [[ $IMAGE_SOURCE == push || $WEIGHTS_SOURCE == push ]]; then
    export REMOTE_PUSH_DIR="$remote/runs/$RUN_ID/incoming"
    # PUSH_DIR must be relative to the node kit's working directory, not the Mac root.
    export PUSH_DIR="runs/$RUN_ID/incoming"
    python3 "$ROOT/watcher/push.py" "$target"
fi
python3 "$ROOT/watcher/transport.py" | ssh "${ssh_args[@]}" "$target" "cd $remote && python3 watcher/remote.py"
notify() { "$ROOT/watcher/notify.sh" "$1" "$2"; }
notify milestone "Node bootstrap started; run $RUN_ID"
# Add the node to the Mac's Duo status page by node_id. The bootstrap is already
# running on the node, this only writes local files, and it can never fail the run.
if [[ ${NODE_REGISTER:-1} == 1 ]]; then
    python3 "$ROOT/watcher/register_node.py" --kit-dir "$remote" -p "$port" -i "$key" "$target" </dev/null || true
fi
bench_started=0
relay_started=0
relay_key=${RELAY_NODE_SSH_KEY_PATH:-$key}
relay_down() {
    if (( relay_started )); then
        if python3 "$ROOT/watcher/relay.py" down "$RUN_ID"; then
            relay_started=0
        else
            notify blocked "Relay cleanup failed for $RUN_ID; inspect relay-side state"
            return 1
        fi
    fi
}
# Normal cleanup is after node drain. Abnormal watcher exit disconnects to avoid an orphan route.
trap relay_down EXIT
while (( $(date +%s) < WINDOW_END )); do
    if ssh "${ssh_args[@]}" "$target" "cat $remote/runs/$RUN_ID/status.json" > "$RUN_DIR/remote.status.json.tmp" 2>/dev/null; then
        mv "$RUN_DIR/remote.status.json.tmp" "$RUN_DIR/remote.status.json"
        phase=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["phase"])' "$RUN_DIR/remote.status.json")
        ready=$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(int(s.get("ready",False) and s.get("ok")=="ok"))' "$RUN_DIR/remote.status.json")
        if [[ $ready == 1 ]] && (( $(date +%s) < WINDOW_END-600 )); then
            if [[ $selected == ssh && $SSH_DIRECTION == forward && $relay_started == 0 ]]; then
                # Mark before calling so a partially failed up is cleaned up by EXIT.
                relay_started=1
                python3 "$ROOT/watcher/relay.py" up "$RUN_ID" "$target" --port "$port" --key "$relay_key" --expires "$WINDOW_END"
                notify milestone "Relay forwarding ready for $RUN_ID"
            fi
            if [[ $bench_started == 0 && ${FIRST_WINDOW:-1} == 1 ]]; then
                bench_started=1
                if mkdir "$ROOT/runs/first-benchmark.lock" 2>/dev/null; then
                    python3 "$ROOT/watcher/transport.py" | ssh "${ssh_args[@]}" "$target" "cd $remote && python3 watcher/start-bench.py"
                    notify milestone "First-window benchmark started; run $RUN_ID"
                fi
            fi
        fi
        if [[ $phase == stopped ]]; then relay_down; fi
    fi
    if ssh "${ssh_args[@]}" "$target" "test -f $remote/runs/$RUN_ID/done"; then
        relay_down
        rsync -az -e "$ROOT/watcher/ssh-rsync.sh" "$target:$remote/runs/$RUN_ID.tar.gz" "$ROOT/runs/"
        notify "done" "Retrieved runs/$RUN_ID.tar.gz"
        exit 0
    fi
    sleep "${POLL_SECONDS:-20}"
done
relay_down
notify blocked "Window expired; relay stopped. Artifact retrieval incomplete: runs/$RUN_ID/remote.status.json"
exit 1

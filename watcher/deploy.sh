#!/usr/bin/env bash
# Remote commands intentionally expand validated local paths and run IDs.
# shellcheck disable=SC2029
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
claim=${1:?normalized claim JSON required}
if [[ ${DRY_RUN:-0} == 1 ]]; then echo 'DRY RUN: validate claim, rsync kit, bootstrap via SSH stdin, poll, benchmark first window, retrieve archive'; exit 0; fi
umask 077
values=()
while IFS= read -r value; do values+=("$value"); done < <(python3 "$ROOT/claim/validate.py" "$claim")
[[ ${#values[@]} == 5 ]] || { echo 'Claim validation failed' >&2; exit 1; }
host=${values[0]}; port=${values[1]}; user=${values[2]}; key=${values[3]}
export WINDOW_END=${values[4]}
export RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM}
[[ $RUN_ID =~ ^[a-zA-Z0-9_-]+$ ]] || exit 1
remote=${NODE_KIT_DIR:-b300-inference}
[[ $remote =~ ^[a-zA-Z0-9_/-]+$ && $remote != /* && $remote != *..* ]] || { echo 'NODE_KIT_DIR must be a safe relative path'; exit 1; }
ssh_args=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -p "$port" -i "$key")
target="$user@$host"
# rsync's -e accepts shell text; use a wrapper, with values in environment instead.
export NODE_SSH_PORT=$port NODE_SSH_KEY_PATH=$key
ssh "${ssh_args[@]}" "$target" "mkdir -p $remote"
rsync -az -e "$ROOT/watcher/ssh-rsync.sh" --exclude='.git/' --exclude='.env*' --exclude='*.env' \
    --exclude='runs/' --exclude='logs/' --exclude='__pycache__/' --exclude='.venv/' --exclude='*.fixture.txt' \
    "$ROOT/" "$target:$remote/"
mkdir -p "$ROOT/runs/$RUN_ID"
python3 "$ROOT/watcher/transport.py" | ssh "${ssh_args[@]}" "$target" "cd $remote && python3 watcher/remote.py"
notify() { "$ROOT/watcher/notify.sh" "$1" "$2"; }
notify milestone "Node bootstrap started; run $RUN_ID"
bench_started=0
while (( $(date +%s) < WINDOW_END )); do
    if ssh "${ssh_args[@]}" "$target" "cat $remote/runs/$RUN_ID/status.json" > "$ROOT/runs/$RUN_ID/remote.status.json.tmp" 2>/dev/null; then
        mv "$ROOT/runs/$RUN_ID/remote.status.json.tmp" "$ROOT/runs/$RUN_ID/remote.status.json"
        phase=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["phase"])' "$ROOT/runs/$RUN_ID/remote.status.json")
        if [[ $phase == serving && $bench_started == 0 && ${FIRST_WINDOW:-1} == 1 ]]; then
            bench_started=1
            # Atomic shared marker: only one first-window benchmark, including overlaps.
            if mkdir "$ROOT/runs/first-benchmark.lock" 2>/dev/null; then
                python3 "$ROOT/watcher/transport.py" | ssh "${ssh_args[@]}" "$target" "cd $remote && python3 watcher/start-bench.py"
                notify milestone "First-window benchmark started; run $RUN_ID"
            fi
        fi
    fi
    if ssh "${ssh_args[@]}" "$target" "test -f $remote/runs/$RUN_ID/done"; then
        rsync -az -e "$ROOT/watcher/ssh-rsync.sh" "$target:$remote/runs/$RUN_ID.tar.gz" "$ROOT/runs/"
        notify "done" "Retrieved runs/$RUN_ID.tar.gz"
        exit 0
    fi
    sleep "${POLL_SECONDS:-20}"
done
notify blocked "Window ended before artifact retrieval; inspect runs/$RUN_ID/remote.status.json"
exit 1

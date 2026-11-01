#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
once=0
[[ ${1:-} != --once ]] || once=1
# shellcheck source=watcher/hooks.sh
source "$ROOT/watcher/hooks.sh"
if [[ -f ${ACCOUNT_CONFIG:-$ROOT/config/account.env} ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${ACCOUNT_CONFIG:-$ROOT/config/account.env}"
    set +a
fi
# Dry run always uses bundled stubs, never operator hooks.
if [[ ${DRY_RUN:-0} != 1 && -n ${PROVIDER_HOOKS:-} ]]; then
    # shellcheck source=/dev/null
    source "$PROVIDER_HOOKS"
fi
pids=()
while :; do
    alive=()
    for pid in ${pids[@]+"${pids[@]}"}; do if kill -0 "$pid" 2>/dev/null; then alive+=("$pid"); else wait "$pid" || true; fi; done
    pids=(${alive[@]+"${alive[@]}"})
    rc=0
    if (( ${#pids[@]} < 2 )); then provider_window_available || rc=$?; else rc=1; fi
    case "$rc" in
        0)
            provider_start_session
            mkdir -p "$ROOT/runs/claims"
            claim="$ROOT/runs/claims/$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM.json"
            provider_claim > "$claim"
            # Each hook must return each allocation once. No account rotation exists.
            "$ROOT/watcher/deploy.sh" "$claim" &
            pids+=("$!")
            ;;
        1) echo 'No window available (or two node handoffs already active)' ;;
        78) "$ROOT/watcher/notify.sh" needs-input 'Hooks not configured; need sanitized grid portal reply and access method';;
        *) "$ROOT/watcher/notify.sh" blocked "Provider hook failed with exit $rc"; exit "$rc";;
    esac
    if (( once )); then
        for pid in ${pids[@]+"${pids[@]}"}; do wait "$pid"; done
        exit 0
    fi
    sleep "${WATCH_INTERVAL_SECONDS:-60}"
done

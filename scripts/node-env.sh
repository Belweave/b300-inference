#!/usr/bin/env bash
# Node entrypoints only. Never source the Mac credential file in the watcher.
set +x
owner_only() {
    python3 - "$1" "$2" <<'PY'
import os, stat, sys
s = os.stat(sys.argv[1])
if s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
    raise SystemExit(sys.argv[2]+' must be owned by the current user with mode 600')
PY
}
export NODE_ENV_FILE=${NODE_ENV_FILE:-$HOME/node.env}
# shellcheck disable=SC2088
if [[ $NODE_ENV_FILE == '~/'* ]]; then export NODE_ENV_FILE="$HOME/${NODE_ENV_FILE:2}"; fi
if [[ ${DRY_RUN:-0} != 1 && -f $NODE_ENV_FILE ]]; then
    owner_only "$NODE_ENV_FILE" NODE_ENV_FILE
    set -a
    # shellcheck source=/dev/null
    source "$NODE_ENV_FILE"
    set +a
fi
# mirror.env holds one header line with a space in it, so it is read as data, never sourced.
# shellcheck disable=SC2088
if [[ ${MIRROR_ENV_FILE:-} == '~/'* ]]; then MIRROR_ENV_FILE="$HOME/${MIRROR_ENV_FILE:2}"; fi
if [[ ${DRY_RUN:-0} != 1 && -n ${MIRROR_ENV_FILE:-} ]]; then
    owner_only "$MIRROR_ENV_FILE" MIRROR_ENV_FILE
    mirror_line=
    IFS= read -r mirror_line < "$MIRROR_ENV_FILE" || [[ -n $mirror_line ]]
    mirror_line=${mirror_line%$'\r'}
    mirror_line=${mirror_line#MIRROR_AUTH_HEADER=}
    case $mirror_line in
        'Bearer '?*) export MIRROR_AUTH_HEADER="Authorization: $mirror_line" ;;
        'Authorization: Bearer '?*) export MIRROR_AUTH_HEADER=$mirror_line ;;
        *) echo 'MIRROR_ENV_FILE line 1 must be "Bearer <token>" or "Authorization: Bearer <token>"' >&2
           unset mirror_line; return 1 ;;
    esac
    unset mirror_line
fi

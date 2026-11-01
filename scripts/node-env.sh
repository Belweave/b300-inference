#!/usr/bin/env bash
# Node entrypoints only. Never source the Mac credential file in the watcher.
set +x
export NODE_ENV_FILE=${NODE_ENV_FILE:-$HOME/node.env}
# shellcheck disable=SC2088
if [[ $NODE_ENV_FILE == '~/'* ]]; then export NODE_ENV_FILE="$HOME/${NODE_ENV_FILE:2}"; fi
if [[ ${DRY_RUN:-0} != 1 && -f $NODE_ENV_FILE ]]; then
    python3 - "$NODE_ENV_FILE" <<'PY'
import os, stat, sys
s = os.stat(sys.argv[1])
if s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
    raise SystemExit('NODE_ENV_FILE must be owned by the current user with mode 600')
PY
    set -a
    # shellcheck source=/dev/null
    source "$NODE_ENV_FILE"
    set +a
fi

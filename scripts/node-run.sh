#!/usr/bin/env bash
# Supervised one-shot entrypoint. Key stays on stdin, never in shell variables.
set +x
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [[ -n ${NODE_RUN_PYTHON:-} ]]; then
    PYTHON=$NODE_RUN_PYTHON
elif [[ ${DRY_RUN:-0} == 1 ]]; then
    PYTHON=python3
else
    PYTHON="$ROOT/.venv/bin/python3"
    # A failed ensurepip can leave an interpreter behind; retry that venv too.
    if [[ ! -x $PYTHON || ! -x $ROOT/.venv/bin/pip ]]; then
        if ! python3 -m venv "$ROOT/.venv" </dev/null >&2; then
            echo 'Could not create the node venv; run: sudo apt-get install -y python3-venv (python3.12-venv on Ubuntu 24.04 also works), then retry.' >&2
            exit 1
        fi
        rm -f "$ROOT/.venv/requirements-node.sha256"
    fi
    requirements_hash=$(sha256sum "$ROOT/requirements-node.txt")
    requirements_hash=${requirements_hash%% *}
    stamp="$ROOT/.venv/requirements-node.sha256"
    if [[ ! -f $stamp || $(< "$stamp") != "$requirements_hash" ]]; then
        "$PYTHON" -m pip install --disable-pip-version-check --no-input -r "$ROOT/requirements-node.txt" </dev/null >&2
        printf '%s\n' "$requirements_hash" > "$stamp"
    fi
fi
exec "$PYTHON" -B "$ROOT/scripts/node_run.py" "$@"

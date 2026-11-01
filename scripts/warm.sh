#!/usr/bin/env bash
# Node-only entrypoint. Credentials must already be exported; no files are sourced.
set +x
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${NODE_RUN_PYTHON:-$ROOT/.venv/bin/python3}
if [[ -z ${NODE_RUN_PYTHON:-} && ! -x $PYTHON ]]; then PYTHON=python3; fi
exec "$PYTHON" "$ROOT/scripts/warm.py" "$@"

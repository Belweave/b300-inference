#!/usr/bin/env bash
# Supervised one-shot entrypoint. Key stays on stdin, never in shell variables.
set +x
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec python3 -B "$ROOT/scripts/node_run.py" "$@"

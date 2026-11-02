#!/usr/bin/env bash
# Read-only bring-up status: GPUs, downloads, stage, logs. Add --loop [seconds] to keep redrawing.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec python3 -B "$ROOT/scripts/node_status.py" "$@"

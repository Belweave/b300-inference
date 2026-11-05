#!/usr/bin/env bash
# Plan / mock rehearsal only. This stub has no live execution mode.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec python3 -B "$ROOT/scripts/kill_window.py" "$@"

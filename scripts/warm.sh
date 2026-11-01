#!/usr/bin/env bash
# Node-only entrypoint. Credentials must already be exported; no files are sourced.
set +x
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec python3 "$ROOT/scripts/warm.py" "$@"

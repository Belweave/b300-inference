#!/usr/bin/env bash
# Production boot in one command: download, warm, serve, with both guards loaded.
# Safe to rerun: when the last run's engine is still up it resumes that run
# instead of tearing it down. --with-tests adds the one-time test suite.
# The classifier key comes on stdin, never in shell variables.
set +x
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
AUTO_RESUME=1 exec python3 -B "$ROOT/scripts/node_run.py" "$@"

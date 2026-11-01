#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/common.sh
source "$ROOT/scripts/common.sh"
if [[ $DRY_RUN == 1 ]]; then echo 'DRY RUN: SSE concurrency sweep 16,32,64,128,256,512,1024; JSON + Markdown'; exit 0; fi
mkdir -p "$RUN_DIR"
python3 "$ROOT/scripts/bench.py"

#!/usr/bin/env bash
set -euo pipefail
KIT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export KIT_ROOT
export MODEL=${MODEL:-deepseek-ai/DeepSeek-V4.1-Flash-1017}
export MODEL_REVISION=${MODEL_REVISION:-1bae0e1088993b9b82de08f59de5be5a34e42170}
export ENGINE=${ENGINE:-sglang}
export RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
[[ $RUN_ID =~ ^[a-zA-Z0-9_-]+$ ]] || { echo 'Invalid RUN_ID' >&2; exit 1; }
export RUN_DIR="$KIT_ROOT/runs/$RUN_ID"
export WEIGHTS_DIR=${WEIGHTS_DIR:-/data/weights/DeepSeek-V4.1-Flash-1017}
export WEIGHTS_SOURCE=${WEIGHTS_SOURCE:-hf}
export PORT=${PORT:-8000}
export ENGINE_PORT=$PORT
export INFER_UPSTREAM_KEY=${INFER_UPSTREAM_KEY:-${ORIGIN_KEY:-}}
export GATEWAY_PORT=${GATEWAY_PORT:-4000}
export GATEWAY=${GATEWAY:-none}
export TUNNEL=${TUNNEL:-cloudflare}
export ENDPOINT="http://127.0.0.1:$ENGINE_PORT"
export SERVER_NAME="b300-$RUN_ID-engine"
export GATEWAY_NAME="b300-$RUN_ID-gateway"
export DRY_RUN=${DRY_RUN:-0}
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "$*" >&2; exit 1; }
status() { python3 "$KIT_ROOT/scripts/status.py" "$1" "$2"; }
need() { command -v "$1" >/dev/null || die "Missing prerequisite: $1"; }

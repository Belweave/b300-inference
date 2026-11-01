#!/usr/bin/env bash
set -euo pipefail
KIT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export KIT_ROOT
export MODEL_VARIANT=${MODEL_VARIANT:-fp8}
case "$MODEL_VARIANT" in
    fp8) default_model=deepseek-ai/DeepSeek-V4.1-Flash-1017; default_revision=1bae0e1088993b9b82de08f59de5be5a34e42170 ;;
    nvfp4) default_model=nvidia/DeepSeek-V4.1-Flash-1017-NVFP4; default_revision=480550d349ede47a048eb65a276c5c01a06256e2 ;;
    *) echo 'MODEL_VARIANT must be fp8 or nvfp4' >&2; exit 1 ;;
esac
export MODEL=${MODEL:-$default_model}
export MODEL_REVISION=${MODEL_REVISION:-$default_revision}
export ENGINE=${ENGINE:-sglang}
export RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
[[ $RUN_ID =~ ^[a-zA-Z0-9_-]+$ ]] || { echo 'Invalid RUN_ID' >&2; exit 1; }
export RUN_DIR="$KIT_ROOT/runs/$RUN_ID"
export WEIGHTS_DIR=${WEIGHTS_DIR:-/data/weights/${MODEL##*/}}
export WEIGHTS_SOURCE=${WEIGHTS_SOURCE:-hf}
export PORT=${PORT:-8000}
export ENGINE_PORT=$PORT
if [[ -z ${INFER_UPSTREAM_KEY:-} && -n ${ORIGIN_KEY:-} ]]; then
    export INFER_UPSTREAM_KEY=$ORIGIN_KEY
    if [[ ${ORIGIN_ALIAS_WARNED:-0} != 1 ]]; then
        echo 'ORIGIN_KEY is deprecated; use INFER_UPSTREAM_KEY. Alias removed next release.' >&2
        export ORIGIN_ALIAS_WARNED=1
    fi
fi
export INFER_UPSTREAM_KEY=${INFER_UPSTREAM_KEY:-}
export GATEWAY_PORT=${GATEWAY_PORT:-4000}
export GATEWAY=${GATEWAY:-none}
export NETWORK_MODE=${NETWORK_MODE:-auto}
export SSH_DIRECTION=${SSH_DIRECTION:-forward}
export IMAGE_SOURCE=${IMAGE_SOURCE:-$([[ $WEIGHTS_SOURCE == push ]] && echo push || echo pull)}
export PUSH_DIR=${PUSH_DIR:-$RUN_DIR/incoming}
export STAGING_DIR=${STAGING_DIR:-$KIT_ROOT/staging}
export ENDPOINT="http://127.0.0.1:$ENGINE_PORT"
export SERVER_NAME="b300-$RUN_ID-engine"
export GATEWAY_NAME="b300-$RUN_ID-gateway"
export DRY_RUN=${DRY_RUN:-0}
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "$*" >&2; exit 1; }
status() { python3 "$KIT_ROOT/scripts/status.py" "$1" "$2"; }
need() { command -v "$1" >/dev/null || die "Missing prerequisite: $1"; }

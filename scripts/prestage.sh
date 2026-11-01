#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=scripts/common.sh
source "$ROOT/scripts/common.sh"
case "$ENGINE" in sglang|vllm) ;; *) die 'Invalid ENGINE';; esac
set -a
# shellcheck source=/dev/null
source "$ROOT/config/$ENGINE.env"
set +a
pin_engine_image
export WEIGHTS_DIR="$STAGING_DIR/$MODEL_VARIANT/weights" WEIGHTS_SOURCE=hf HF_XET_HIGH_PERFORMANCE=1
if [[ $DRY_RUN == 1 ]]; then
    cat <<PLAN
DRY RUN: prestage only; no downloads, Docker, or large files created.
Model variant: $MODEL_VARIANT ($MODEL)
Pinned revision: $MODEL_REVISION
Weights: STAGING_DIR/$MODEL_VARIANT/weights; HF Xet, 16 workers; verify committed file hashes.
Image: $ENGINE_IMAGE
Plan: docker save IMAGE | zstd -T0 -3 > STAGING_DIR/$MODEL_VARIANT/images/engine.tar.zst
Record archive SHA256, byte size, image ID, registry digest, and platform in engine.json.
PLAN
    exit 0
fi
umask 077
need python3; need docker; need zstd
python3 "$ROOT/scripts/weights.py"
python3 "$ROOT/scripts/images.py" stage
log 'Prestage complete; no node contacted'

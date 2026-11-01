#!/usr/bin/env bash
# Detached node commands load the same secrets as their redactor, before logging.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=scripts/node-env.sh
source "$ROOT/scripts/node-env.sh"
log=${1:?log path required}; shift
umask 077
"$@" 2>&1 | python3 "$ROOT/scripts/redact.py" >> "$log"

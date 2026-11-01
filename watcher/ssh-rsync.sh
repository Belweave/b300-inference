#!/usr/bin/env bash
set -euo pipefail
exec ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 \
    -p "${NODE_SSH_PORT:?}" -i "${NODE_SSH_KEY_PATH:?}" "$@"

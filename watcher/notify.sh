#!/usr/bin/env bash
set -euo pipefail
printf '%s: %s\n' "$1" "$2"
if [[ ${DRY_RUN:-0} != 1 && -x $HOME/.config/kai/kai-post ]]; then
    "$HOME/.config/kai/kai-post" "$1" "$2" 'B300 inference window' || true
fi

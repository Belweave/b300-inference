#!/usr/bin/env bash
set -euo pipefail
# No provider/network calls. 78 = not configured; 1 = no window; 0 = ready.
provider_window_available() { echo 'provider_window_available: not configured' >&2; return 78; }
provider_get_ssh_target() { echo 'provider_get_ssh_target: not configured; use claim JSON' >&2; return 78; }
provider_start_session() { echo 'provider_start_session: not configured (no allocation requested)' >&2; return 78; }
provider_claim() {
    # TODO: pipe the real portal reply to claim/parse.py after parser implementation.
    echo 'provider_claim: not configured; sanitized portal reply needed' >&2
    return 78
}

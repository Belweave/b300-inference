Provided byte-for-byte by Preetham from Netops on 2026-10-31. Do not edit locally.

node-tunnel.sh SHA256:
845edd39cb7c91bf5dbcff06b09516833aafbd893ec9ddc42710cbd140fca498

Pins cloudflared 2026.10.7 and checks the amd64/arm64 binary SHA256. Its public
interface consumes CF_TUNNEL_TOKEN from env; it passes the token to cloudflared
through child env, never argv. Bootstrap performs no token-name mapping.

This Netops revision forces xtrace off before using secrets. It reuses an already
SHA256-verified `/tmp/cloudflared`; when missing or mismatched it downloads to
`/tmp/cloudflared.new`, checks the architecture-specific hash, then chmods and
renames it over the path. That avoids writing to the running binary inode during
replica handoff (the old text-file-busy failure). The script is executed, never
sourced. It exports `TUNNEL_TOKEN` and unsets `CF_TUNNEL_TOKEN` inside its own
process; this does not clear the invoking shell/bootstrap environment.

The helper **appends** to `/tmp/cloudflared.log`. Bootstrap records the file's
identity and byte offset before each launch. Teardown filters only the suffix
from that boundary, after requesting connector stop; it omits shared content if
the cursor is absent or the file was replaced/truncated. Overlapping replicas
can still interleave within that suffix, so this is a time boundary, not exclusive
per-PID attribution. Current environment secrets and long Cloudflare token-shaped
strings are redacted; archive members pass through the filter again. The helper
itself still writes a raw shared log, which must be inspected carefully.

Remaining considerations: shared /tmp/cloudflared and raw /tmp/cloudflared.log,
PID returned on stdout rather than a pidfile, and tunnel registration/graceful
shutdown still need runtime qualification. No tunnel was opened during development.
Netops' relay.sh is a separate component; it is neither vendored nor duplicated here.

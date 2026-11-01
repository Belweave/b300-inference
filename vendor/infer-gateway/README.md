Provided byte-for-byte by Preetham from Netops on 2026-10-31. Do not edit locally.

node-tunnel.sh SHA256:
d04775930a6a4bcc36657a1c86cd641f2a54fffac4855f8be983d3b3e18dba7a

Pins cloudflared 2026.10.7 and checks the amd64/arm64 binary SHA256. Its public
interface consumes CF_TUNNEL_TOKEN from env; it passes the token to cloudflared
through child env, never argv. Bootstrap performs no token-name mapping.

Remaining considerations: shared /tmp/cloudflared and raw /tmp/cloudflared.log,
PID returned on stdout rather than a pidfile, and tunnel registration/graceful
shutdown still need runtime qualification. No tunnel was opened during development.
Netops' relay.sh is a separate component; it is neither vendored nor duplicated here.

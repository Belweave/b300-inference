Provided verbatim by Preetham from Netops in the task conversation on 2026-10-31.
Do not edit node-tunnel.sh locally. No tunnel has been opened during kit development.

Known upstream issues: downloads unpinned linux-amd64 latest without a checksum;
passes the token in argv; writes a raw log to /tmp; supplies no pidfile or drain API.
Ask Netops for a pinned, checksum-verified architecture-aware build, TUNNEL_TOKEN
consumption from the environment, sanitized logs, and an explicit shutdown contract.

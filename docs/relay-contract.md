# Node kit ↔ Netops relay contract

Netops owns `relay.sh` in its **infer-gateway** repository. This kit calls its
interface; it neither vendors that file nor implements its forward tunnel or
relay-side cloudflared. `RELAY_SH` is the configured executable path, not a copy
in this repository. All credentials are runtime environment values or existing
SSH key paths, never inline key material.

## Inputs and identity

The claim adapter returns `host`, `port`, `user`, `key` (absolute path or env
reference resolving to a path), and `expires_at` (UTC epoch seconds). The parser
is still a stub awaiting a sanitized real portal reply. A stable `window-id` is
`window-` plus the first 20 hex digits of SHA256 of canonical JSON containing
host/port/user/expires_at. Key paths are excluded so Mac/relay path differences
do not change allocation identity. Retries of a claim use the same ID.

The Mac must be able to SSH to the node. The relay also needs inbound SSH access
to it. Both direct transport and Netops' updated helper honor the claim's SSH
`port`. Node ingress is SSH only; no public inference ports are needed.

## Ready before up

The automated kit binds **127.0.0.1:8000**. Bootstrap waits for these checks in order:

1. `GET http://127.0.0.1:8000/health` **without Authorization** → 200.
2. Unauthenticated `GET /v1/models` → 401, then the same request with
   `Authorization: Bearer <INFER_UPSTREAM_KEY>` → 200 with the configured model.
3. A real authenticated `POST /v1/chat/completions` → generated text and tokens.

SGLang's inspected auth middleware exempts `/health*`; vLLM v0.32.0 guards
`/v1`, `/v2`, `/inference`, `/cohere`, leaving `/health` exempt. Both mount health
on the main API app/port. No extra exemption flag or plaintext-key curl argument
is needed. [Source evidence and test scope](health-contract.md) records versions.
The moving SGLang preview image is qualified again by these checks on each node.

In SSH mode, `runs/<window-id>/status.json` then has `phase: "ready"`, `ok: "ok"`,
`ready: true`, and `network.selected: "ssh"`. This is **node readiness**, not proof
of public routing. The watcher calls `up` only after this state and before T−10m.
Netops' helper has only a 60-second health wait; it must not be started during model
loading. Bootstrap also records `network.requested`, `network.ssh_direction`, and
per-host HTTPS probe results. Explicit SSH mode does not probe outbound HTTPS.

## Exact interface

For a local relay:

```text
RELAY_SH up <window-id> <user@node-host> --port <claim-port> --key <node-key-path> --expires <expires_at>
RELAY_SH down <window-id>
RELAY_SH push-weights <user@node-host> <local-weights-dir> <remote-dir> --port <claim-port> --key <node-key-path>
```

These calls match Netops' updated flag interface exactly. The Mac invokes the
external executable via argv arrays. Set `RELAY_HOST=user@relay` to dispatch the
same argv over SSH to that host; `RELAY_SH` must be an absolute path there.
`RELAY_SSH_KEY_PATH` optionally selects the Mac→relay identity.
`RELAY_NODE_SSH_KEY_PATH` is required for the relay→node key path on that host.
The remote dispatcher uses Python 3 and passes `CF_TUNNEL_TOKEN` (if supplied) and
`RELAY_STATE` through SSH stdin into process env, never command text or a file.
Alternatively provision the token in the relay's runtime environment. External
helper output is not echoed by the wrapper because it may contain credentials.

The relay itself chooses:

```text
relay_port = first unused/unclaimed port in 18000..18999
port_file  = <state>/<window-id>.port
state      = ${RELAY_STATE:-$HOME/.b300-relay}
forward    = ssh ... -L 127.0.0.1:<relay_port>:127.0.0.1:8000 <user@node-host>
```

The wrapper records the helper's chosen port (from its success line or `.port`
file, including on remote relays) in `runs/<window-id>/relay.status.json`, alongside
the node SSH port and expiry. `down` marks that status stopped, retaining the port
for diagnostics. It never predicts a port from the window ID.

The relay checks cloudflared **2026.10.7**, reserves an available port in its state,
and uses `--info=progress2` only with rsync ≥3.1 (otherwise `--progress`).
The relay dials **into** the node, with keepalive, ExitOnForwardFailure, and the
helper's reconnect loop. Its cloudflared points at the relay-local listener.
Public chain: `infer.belweave.tech` Worker → `infer-origin.belweave.tech` → named
tunnel **b300-infer** → relay listener → node loopback. Confirm with Netops that
named-tunnel remote ingress honors the intended per-replica local port; do not
assume a `--url` flag overrides a remotely managed ingress configuration.

## Staging paths

`STAGING_DIR/<MODEL_VARIANT>/weights` is the Mac-side staged snapshot;
`STAGING_DIR/<MODEL_VARIANT>/images/{engine.tar.zst,engine.json}` holds the image.
For a remote relay, `RELAY_STAGING_DIR/<MODEL_VARIANT>/weights` must already exist
on that relay; the wrapper passes that path to `push-weights`. It does not create
a second 510 GB copy through the Mac. The compressed image is pushed from the
Mac over SSH independently. For relay-only image storage, Netops must provide a
staging/copy path separately; this draft interface has no push-image operation.

Destination defaults to `/data/weights/<model-repo-name>` (`WEIGHTS_DIR`). The kit
creates node staging directories over SSH, calls `push-weights`, transfers the
image, and only then starts bootstrap. Without `RELAY_SH`, bounded parallel rsync
workers split the committed file list, use `--partial`, and copy disjoint files.
That fallback handles staging only; SSH **forward serving** still needs Netops'
relay component. Transfer operations are bounded to leave at least eleven minutes
before the claim expiry.

Netops' copy does size/mtime synchronization, not integrity verification. The node
verifies every file against `docs/model-metadata.json` (size + SHA256/LFS for large
files, size + Git blob SHA1 for small files) before serving. No HF metadata lookup
is needed on the node. A corrupt same-size/same-mtime file may need explicit repair
on a subsequent Netops rsync retry; a mismatch never starts the engine.

## Drain, handoff, expiry

The same token joins new windows as replicas. Two concurrent windows use two
stable IDs, two relay-local ports, and two cloudflared connectors. Up the new
window only when it is ready; keep the old one until its drain/teardown. Grid
permission for overlapping allocations remains required.

At `expires_at - 600`, the node collects diagnostics, performs bounded drain,
stops the engine, and writes `phase: "stopped"`. The watcher then calls
`down <window-id>` (also on the completion marker), before artifact retrieval.
The watcher tears down partial relay starts on error and calls down at expiry
if status polling never completes. Default polling can leave a dead-origin gap
up to `POLL_SECONDS` (20) plus command latency after engine stop. Netops should
add health-aware removal/admission signaling for a stronger handoff guarantee.
No replica-ready assumption is made from a process PID.

Node teardown has a detached deadline timer. The watcher passes the original
claim expiry with `--expires`; the updated helper self-tears-down there even if
the Mac disappears. Normal drain/down still happens at T−10m. Relay process
supervision across SSH disconnects/reboots and idempotent retry cleanup still
need qualification. The kit does not release or bill the grid allocation.

## Optional reverse SSH

`NETWORK_MODE=ssh SSH_DIRECTION=reverse` is explicitly opt-in. Supply
`REVERSE_RELAY_TARGET=user@relay`, `REVERSE_RELAY_PORT`, and optionally
`REVERSE_SSH_KEY_PATH` (an existing key on the node, never copied by this kit).
After readiness the node starts `scripts/reverse-tunnel.sh`:

```text
ssh -N -R 127.0.0.1:<relay_port>:127.0.0.1:8000 <user@relay>
```

It uses BatchMode, strict host keys, keepalives, ExitOnForwardFailure, reconnects,
and stops at T−10m or a node stop marker. It requires outbound SSH and relay-side
TCP forwarding permission; it cannot help a node with no outbound traffic at all.
The draft `relay.sh up` always starts `-L`, so the watcher deliberately does not
call it for reverse mode: doing so could conflict with the `-R` listener. Netops
must separately provision/supervise the relay-side cloudflared for this sub-mode.
Reverse readiness means transport/node ready only; public service is not automatic.
Stopping the reverse transport at T−10m can interrupt streams; qualify its drain
semantics before using it for public traffic.

## Remaining Netops checks

* Keep node-ready-before-up; the helper's 60-second health wait is too short for cold loading.
* Verify remotely managed ingress versus per-window `--url`, health-aware withdrawal,
  Worker key propagation, and public SSE behavior during replica handoff.
* Add checksum/repair handling to push-weights; node verification remains mandatory.
* Qualify up/down idempotency, failure rollback, PID ownership checks, and process
  supervision across remote SSH disconnects/reboots. Sequential free-port selection
  is implemented; simultaneous reservations still need an atomic allocation test.
* Verify the installed relay cloudflared binary's provenance/checksum in addition
  to the now-enforced version. The node helper already checks its download hash.
* Replace accept-new with independently provisioned host keys where practical,
  and quote key paths safely (the helper's shell-word expansion mishandles spaces).
* Supply the separate reverse-mode connector before enabling reverse serving.

## Live launch and model identity

The [live-node recipe](live-node.md) uses `0.0.0.0:8000` inside Docker with
host port 8000 published on loopback for the tunnel. The running upstream serves
`/models/dsv41`; authenticated direct model-list/chat checks must expect that name.
The gateway advertises `deepseek-ai/DeepSeek-V4.1-Flash-1017` and maps it to
`/models/dsv41`, while accepting the upstream name unchanged (including Hermes).
The future clean restart uses the catalog served name and drops the gateway
rename; see [the naming contract](model-naming.md). Netops owns gateway changes.
The live key comes from `~/infer.key` and remains visible in container/process
inspection. Sed redaction and Docker's `--log-driver none` protect logs only;
see [the auth contract](auth-contract.md).

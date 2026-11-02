# Supervised B300 bring-up

Run from the operator machine, with the existing classifier key as stdin:

```sh
ssh <node> 'cd ~/b300-inference && ./scripts/node-run.sh' < /path/to/classifier-key
```

Chief Engineer supervises the whole run. There is no retry loop or container
restart policy. The only allowed relaunch is the existing warm fit fallback to
64. The script never releases the allocation, including on failure. It always
prints a manual teardown deadline; the operator must act by that time. Success
leaves the engine, guards, relay and tunnel running for Netops. A failure after
warm leaves the engine up too, so it can be resumed (below).

Before running, provision the existing mode-600, current-user-owned node env
(`NODE_ENV_FILE`, default `~/node.env`). It needs `INFER_UPSTREAM_KEY`,
`TUNNEL_SLOT=a` or `b`, and **both actual 40-character model commit SHAs** in
`SAFETY_MODEL_REVISION` and `SAFETY_QWEN_REVISION`. The repository has no verified
pins for these two guard models. Policy labels, `main` and `unpinned` are refused.
The launcher passes those revisions to SGLang and the relay reports the same
revisions. The defaults are Shieldstral 1.0 3B and Qwen3Guard-Gen-0.6B.

Set `PROVISIONED_AT` to the original provisioning time (epoch seconds or ISO with
a timezone). Defaulting to invocation time is suitable only for a newly
provisioned node. The deadline is provisioning + `CREDIT_CAP_SECONDS` (11250) −
600 seconds. An existing warm cutover bound reserves another 180 seconds. A
missing upstream key or tunnel slot fails before downloads. Docker, its GPU
runtime, Python dependencies and the checked-out kit must already be installed.

The classifier key is read once, from the first stdin line, with CR/LF removed.
Empty input and a terminal are refused. It is passed to warm through a pipe and
placed only in the relay process environment. It never enters argv or a file.
The runner makes authenticated requests in-process and redacts subprocess logs.
A final literal scan of `runs/` fails if the key appears anywhere. Keep `runs/`
free of symlinks so that the scan can certify it.

Each gate prints `PASS <gate>` or `FAIL <gate>: <reason>`. Any failure stops and
saves `runs/node-run-<UTC timestamp>.json` with timings and the evidence collected
so far. The report includes the image digest, git SHA, warm READY signal and run
directory, guard configuration, measurements and final decision.

1. **preflight:** load node env quietly; require exactly eight B300s, Docker and
   aiohttp; fetch and verify cloudflared (see Tunnel); clean up after earlier
   runs (see Cleanup); check ports 8000, the engine port (8001), 8600 and 8601;
   require a pinned engine image and guard revisions; check the weight
   filesystem. `ALLOW_UNPINNED` cannot permit a launch.
2. **warm-timeout:** use `WARM_TIMEOUT_SECONDS=3600` unless explicitly configured;
   cap it by the remaining absolute deadline, including warm's earlier bound.
3. **key:** read the piped key without prompting or echoing.
4. **warm:** foreground `warm.sh --layout tp8`; require one successful READY
   signal and matching pinned image. Its existing KV/OOM fallback may select 64.
   This path uses only `hello world` for warm smoke inputs and skips prefix replay.
5. **guards:** launch both detached guard containers from that same image,
   bound to loopback 8600/8601. Measure per-GPU memory before/after and collect
   compute-process memory when supported. Both guards must become healthy, then
   pass the guard benchmark (below).
6. **relay-safety:** for both phases, require benign authenticated false,
   unauthenticated 401, and the existing inert canary true.
7. **kv-pool:** parse the engine log using `warm_logic.parse_pool`; require both
   the token pool size and engine running-request limit.
8. **loadtest:** use the live client at 130 rps, concurrency 128 (or warm's 64),
   for 60 seconds. The existing 10% input/90% output mix uses repeated
   `hello world`, 16000/640 characters, and one canary per 50 calls. Check shape
   and canary flag, allowing real scores to vary. Queueing and response decoding
   count toward the three-second deadline. Non-200s and invalid results stop
   immediately. A p99 miss at 128 is recorded for the fit decision, without
   printing a failed gate and then continuing.
9. **fit:** require shared KV-fit math, no engine clamp, at least 4096 MiB free
   on **every TP8 rank**, and p99 strictly below 100 ms. A 128 miss triggers one
   engine-only warm relaunch at 64 using cached weights and the smaller graph
   ladder. The relay and guards stay running. Recheck KV, safety, load and rank
   headroom. Any miss at 64 fails; warm's permissive floor warning is insufficient.
10. **tunnel:** start Netops' helper and wait for a registered connection, then
    a keyless probe through the public URL (see Tunnel). This is a hard gate.
11. **handoff:** print exactly one `NODE READY FOR DRILL ...` line with final fit,
    p99, observed total guard VRAM change and report path.
12. **key-leak:** scan all `runs/` files for the literal key, without passing the
    search pattern through argv or printing it. Also check on failure.

Useful overrides: `RELAY_PUBLIC_URL`, `WEIGHTS_DIR`, `GUARD_WEIGHTS_DIR`,
`GUARD_GPU=0`, `GUARD_SHIELD_MEM_FRACTION=0.06`, `GUARD_QWEN_MEM_FRACTION=0.02`,
`GUARD_TIMEOUT_SECONDS=600`, `GUARD_BENCH_ROUNDS=8`, `TUNNEL_CONNECT_TIMEOUT=180`,
`CLOUDFLARED_SHA256`, `FIT_RANK_HEADROOM_MIB=4096`,
`FIT_TOKENS_PER_STREAM=32768`, `LOADTEST_DURATION_SECONDS=60`.
Disk planning defaults to `MIN_FREE_GB=550` minus existing manifest files, with
at least 20 GB free. `DISK_SPACE_ACTION=warn` permits proceeding below that
threshold; the default is `fail`. Existing file sizes reduce the estimate only;
warm still verifies checksums. Allow separate space for Docker layers and guards.

`DRY_RUN=1 ./scripts/node-run.sh` prints the gate plan without reading node env or
stdin, probing hardware/network, or creating files. Test-only overrides require
`NODE_RUN_TESTING=1`: `EXPECT_GPU_NAME`, `EXPECT_GPU_COUNT`,
`NODE_RUN_WARM_CMD`, `NODE_RUN_RELAY_URL`, `NODE_RUN_GUARD_HEALTH_URL`,
`NODE_RUN_TUNNEL_PUBLIC_URL`, `NODE_RUN_ENGINE_HEALTH_URL`, `CLOUDFLARED_BIN`,
`CLOUDFLARED_LOG`, `CLOUDFLARED_GITHUB_URL`. Do not set these on the paid node.

The offline default of `safety_loadtest.py` remains available. Its live CLI is
`python3 scripts/safety_loadtest.py --url http://127.0.0.1:8000 --json`, with the
key already in `SAFETY_CLASSIFIER_KEY`, never a command-line argument.

The rehearsal uses fake GPU/Docker/warm processes and a loopback aiohttp server
with mocked verdicts. It downloads no models and proves no model accuracy or
B300 latency. The pinned engine's support for both guard architectures, startup
flags, guard cache access and memory fractions still needs supervised node
qualification. A failed guard startup or final fit produces no handoff. GPU
memory deltas can include concurrent serving-engine allocations; the final
headroom check reads actual free memory again after load.

## Resume after a late failure

Before this change, any failure after warm meant another 10 to 13 minute warm,
because nothing could pick up a running engine. Now a failure in guards,
relay-safety, kv-pool, loadtest, fit or tunnel leaves the engine running and
prints the command to continue:

```
ENGINE KEPT: the engine is still up. Fix the cause, then rerun only the remaining gates:
  RESUME=node-run-20261102T130000.000000Z ./scripts/node-run.sh < /path/to/classifier-key
```

With `RESUME=<run id>` the run gates are `preflight`, `key`, `resume`, then the
late gates, `handoff` and `key-leak`. They append to the same
`runs/<run id>.json`. `resume` fails closed unless all of these hold:

- the engine container named in the warm READY signal exists and is running;
- its image ID matches warm's report, and warm's report matches the pinned image;
- it mounts the same `WEIGHTS_DIR` at `/weights`, and the READY revision matches
  `config/model-manifest.json`;
- `GET /health` on the engine port returns 200.

A resume keeps the original teardown deadline, never a later one. It skips
cleanup, the port check, the disk check and warm. Guard containers that are
still running and healthy are reused; a dead one is removed and relaunched. A
cloudflared that is still running is reused, and the tunnel gate waits for its
registered connection again. The fit fallback to 64 can still relaunch through
warm.sh. If that relaunch fails, warm removes the engine and there is nothing
to resume.

## Cleanup

A fresh run (not a resume) starts clean. After taking the run lock, preflight
removes every container named `b300-warm-*` or `node-run-*`, and stops any
`sse_relay.py` from this checkout run by the same user. It then checks ports
8000, 8001, 8600 and 8601. If one is still held, it fails and names the port,
for example `port 8000 (relay) is held by a process that is not ours`. The
report lists what was removed under `cleanup`.

## Guard benchmark

Right after both guards are healthy, each one gets `GUARD_BENCH_ROUNDS` (8) passes
over four benign sentences, then the drill canary `CANARY-CSAM-TEST-7f3a`. No
other input is used. The whole step is capped at 45 seconds. `guard_bench` in
the report has, per guard: requests, p50 and p99 latency (nearest rank, so p99
of 32 samples is close to the slowest), benign texts flagged, the model's own
score for the canary and the VRAM it added on its GPU (nvidia-smi before and
after its launch). It also records the relay pipeline's canary verdict for both
phases. A guard that returns no valid score, or a canary that is not flagged,
fails the guards gate. A flagged benign text prints a warning only.

## Offline rehearsal

`make dry-run` prints the plans, then runs node_run end to end with fake GPUs,
Docker, warm and a loopback server for the guards and relay. Verdicts are
mocked. The run fails at the tunnel, keeps the engine, and is resumed through
guards, the guard benchmark, relay-safety, loadtest, fit and tunnel. CI
(`.github/workflows/ci.yml`) runs lint, the unit tests and `make dry-run` on
each pull request. None of it proves B300 latency, model accuracy or that the
pinned image can serve the guards.

## Tunnel (required)

From the Mac, before running node-run.sh:

```
scp -P <port> -p vendor/infer-gateway/node-tunnel.sh <user@node>:~/
```

`~/node.env` (mode 600) must define a non-empty `CF_TUNNEL_TOKEN`. Set it through a masked card; never paste the value in chat. node-run.sh sources it like Netops does (`set -a; . ~/node.env; set +a`) without echo, and passes the token only to `~/node-tunnel.sh`.

Preflight hard-fails before any download or docker pull if `~/node-tunnel.sh` is missing or not executable, `CF_TUNNEL_TOKEN` is missing or empty, `sha256sum` is missing, or no verified cloudflared can be staged.

**cloudflared.** Preflight puts cloudflared 2026.10.7 at `/tmp/cloudflared` and checks its sha256 against the pin in `scripts/node_tunnel.py` (`CLOUDFLARED_SHA256` overrides it). It reuses a binary that already matches. Otherwise it downloads `$MIRROR_URL/tools/cloudflared/2026.10.7/cloudflared-linux-amd64` with the same `MIRROR_AUTH_HEADER` as the model download (no redirects followed), and only if that fails or does not match, the GitHub release of the same version. A file that does not match is deleted and never run. Netops' helper then sees a binary that matches its own pin and skips its download.

**Readiness.** After fit, the tunnel gate runs the helper (60 s cap), then waits up to `TUNNEL_CONNECT_TIMEOUT` (180 s) for `Registered tunnel connection` in the part of `/tmp/cloudflared.log` written after launch, failing early if cloudflared exits. Then it probes the public URL without credentials: a `POST /v1/safety/csam` that the relay answers with 401, or with `PRIVATE_UNGUARDED=1`, a `GET /v1/models` that the engine answers with 401. It prints `TUNNEL UP url=...` or fails the gate with the reason (`tunnel helper failed`, `helper timeout`, `cloudflared exited before registering a connection`, `registered no edge connection`, or `connected but the public URL never reached the relay`). A tunnel failure keeps the engine and prints the RESUME command. `/tmp/cloudflared.log` is raw and unredacted; do not share it.

**Why it said `TUNNEL NOT STARTED reason=timeout` before.** That one reason covered three different cases: no time left before the deadline, a helper that ran past the wait, and a launched tunnel whose public probe never passed. The probe only accepted a relay 401 on `/v1/safety/csam`. With `PRIVATE_UNGUARDED=1` the relay runs without SAFETY and has no such route, so the request passed through to the engine, which does not answer with the relay's `{"error": "unauthorized"}` 401. A working tunnel would still report `timeout`. The 180 s wait also included the helper's cloudflared download from GitHub. Now the download happens in preflight, readiness comes from cloudflared's own log, private runs probe a path the engine answers, and each case has its own reason. On the next node, check that the engine really answers `401` to a keyless `/v1/models` through the relay.

## Time budget (estimates, 3.125 h = 187.5 min credit cap)

| Phase | Best | Worst |
|---|---|---|
| Image pull | 3 | 10 |
| Weights, 510 GB (observed 4 to 10 min on this provider; 60 min WARM_TIMEOUT_SECONDS stays the backstop) | 4 | 10 |
| Engine load + graph capture | 8 | 20 |
| Guards start | 3 | 8 |
| Safety checks + load test | 2 | 3 |
| 64 fallback relaunch | 0 | 20 |
| Tunnel up | 1 | 3 |
| Netops drill | 15 | 30 |
| Teardown margin | 10 | 10 |
| **Total (min)** | **46** | **114** |

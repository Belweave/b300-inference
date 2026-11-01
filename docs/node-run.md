# Supervised B300 bring-up

Run from the operator machine, with the existing classifier key as stdin:

```sh
ssh <node> 'cd ~/b300-inference && ./scripts/node-run.sh' < /path/to/classifier-key
```

Chief Engineer supervises the whole run. There is no retry loop or container
restart policy. The only allowed relaunch is the existing warm fit fallback to
64. The script starts no tunnel and does not release the allocation, including
on failure. It always prints a manual teardown deadline; the operator must act
by that time. Success leaves the engines and relay running for Netops.

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
   aiohttp; check ports 8000, the engine port (8001), 8600 and 8601; require a
   pinned engine image and guard revisions; check the weight filesystem.
   `ALLOW_UNPINNED` cannot permit a launch.
2. **warm-timeout:** use `WARM_TIMEOUT_SECONDS=3600` unless explicitly configured;
   cap it by the remaining absolute deadline, including warm's earlier bound.
3. **key:** read the piped key without prompting or echoing.
4. **warm:** foreground `warm.sh --layout tp8`; require one successful READY
   signal and matching pinned image. Its existing KV/OOM fallback may select 64.
   This path uses only `hello world` for warm smoke inputs and skips prefix replay.
5. **guards:** launch both detached guard containers from that same image,
   bound to loopback 8600/8601. Measure per-GPU memory before/after and collect
   compute-process memory when supported. Both guards must become healthy.
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
10. **key-leak:** scan all `runs/` files for the literal key, without passing the
    search pattern through argv or printing it. Also check on failure.
11. **handoff:** print exactly one `NODE READY FOR DRILL ...` line with final fit,
    p99, observed total guard VRAM change and report path. Remind Netops to run
    `~/node-tunnel.sh` for `b300-infer`, then print local and UTC teardown times.

Useful overrides: `RELAY_PUBLIC_URL` (default
`https://infer-origin.belweave.tech`), `WEIGHTS_DIR`, `GUARD_WEIGHTS_DIR`,
`GUARD_GPU=7`, `GUARD_SHIELD_MEM_FRACTION=0.06`, `GUARD_QWEN_MEM_FRACTION=0.02`,
`GUARD_TIMEOUT_SECONDS=600`, `FIT_RANK_HEADROOM_MIB=4096`,
`FIT_TOKENS_PER_STREAM=32768`, `LOADTEST_DURATION_SECONDS=60`.
Disk planning defaults to `MIN_FREE_GB=550` minus existing manifest files, with
at least 20 GB free. `DISK_SPACE_ACTION=warn` permits proceeding below that
threshold; the default is `fail`. Existing file sizes reduce the estimate only;
warm still verifies checksums. Allow separate space for Docker layers and guards.

`DRY_RUN=1 ./scripts/node-run.sh` prints the gate plan without reading node env or
stdin, probing hardware/network, or creating files. Test-only overrides require
`NODE_RUN_TESTING=1`: `EXPECT_GPU_NAME`, `EXPECT_GPU_COUNT`,
`NODE_RUN_WARM_CMD`, `NODE_RUN_RELAY_URL`, `NODE_RUN_GUARD_HEALTH_URL`.
Do not set these on the paid node.

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

## Tunnel preamble (required)

From the Mac, before running node-run.sh:

```
scp -P <port> -p vendor/infer-gateway/node-tunnel.sh <user@node>:~/
```

`~/node.env` (mode 600) must define a non-empty `CF_TUNNEL_TOKEN`. Set it through a masked card; never paste the value in chat. node-run.sh sources it like Netops does (`set -a; . ~/node.env; set +a`) without echo, and passes the token only to `~/node-tunnel.sh`.

Gate 1 (preflight) hard-FAILs before any download or docker pull if `~/node-tunnel.sh` is missing or not executable, or `CF_TUNNEL_TOKEN` is missing or empty. It prints what is missing and the teardown time.

After `NODE READY FOR DRILL`, node-run.sh starts `~/node-tunnel.sh` detached (redacted log in the run dir) and polls the public origin for up to 180 s. Proof of reachability is a keyless POST to `/v1/safety/csam` that the relay answers with 401. It prints one of:

```
TUNNEL UP url=https://infer-origin.belweave.tech
TUNNEL NOT STARTED reason=<launch failed|timeout>
```

A tunnel failure never tears down the node, changes the fit, or changes the exit status; it is recorded in the report. `/tmp/cloudflared.log` is written by Netops' helper, raw and unredacted; do not share it.

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

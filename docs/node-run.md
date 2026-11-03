# Supervised B300 bring-up

Run from the operator machine, with the existing classifier key as stdin:

```sh
ssh <node> 'cd ~/b300-inference && ./scripts/boot.sh' < /path/to/classifier-key
```

## One-command boot

`./scripts/boot.sh` is the production boot: download, warm, serve.

1. **Download.** Guard weights in preflight, main weights in warm. Both come
   straight from the R2 mirror bucket over S3, with `User-Agent: b300-inference`,
   and every file is checked for size and SHA256 against the pinned manifest.
   Files that already verify are skipped.
2. **Warm.** The pinned TP8 engine, exactly as `node-run.sh` has always run it.
3. **Serve.** Tunnel, then both guards. The guards always load; there is no
   unguarded mode, and `PRIVATE_UNGUARDED` is refused in preflight. Handoff then
   asks each guard for `GET /health` with a 2 second timeout. Any guard that is
   slow, down or not 200 fails the run closed: no `NODE READY`, and the relay keeps
   answering 503 for checks that need a missing guard.

Nothing is tested unless asked. `--with-tests` (or `RUN_TESTS=1`, same as
`--calibrate` and `CALIBRATE=1`) adds the one-time suite below.

Rerunning is safe. If the newest run's engine container is still running,
boot.sh prints `RESUMING <run id>` and resumes that run (see Resume) instead of
cleaning up and warming again. A run that already reached ready only re-verifies
the guards, tunnel and handoff, even if it was a test run. If the engine is gone,
boot.sh starts a fresh run. `node-run.sh` is the same runner without the
automatic resume.

Netops' boot-time credentials arrive as files, never arguments. The read-only
mirror creds go in the file named by `MIRROR_S3_ENV_FILE`, or `~/mirror-s3.env`
when node.env names none (mode 600, owned by you, parsed as data). The SGLang
admin key needs nothing from anyone: serve_group makes a random one inside the
container on every launch. None of these values are printed or written to `runs/`.

## Steady boot and calibration

`./scripts/node-run.sh` is the steady boot: preflight (which downloads and
verifies both guards), warm (which downloads and verifies the main model), tunnel,
guards, handoff. It tests nothing. The guards always load, from the verified
weights, and the second one starts once the first has loaded, because each sizes
its memory against what is free on GPU 0. The run waits for each to load, and
handoff checks both again with a 2 second timeout. Until both
answer, the relay returns 503 for any check that needs a missing guard, so
traffic is refused, never served unguarded. Downloads keep their SHA256 checks.
They hash while streaming, and a rerun reads markers, so they add no extra pass.

`./scripts/node-run.sh --calibrate` (or `--with-tests`, `CALIBRATE=1`, `RUN_TESTS=1`) runs the one-time suite
described below: guard benchmark and canary, relay-safety, kv-pool, load test,
fit with the single 64 fallback, and the public tunnel probe. Run it by hand for
a new node type, guard revision, engine image or serving config. Nothing starts
it automatically. Each report has `mode` (`steady` or `calibrate`) and a
`fingerprint` of what the run used: GPU model and count, engine image digest, guard
revisions, and a hash of `scripts/guards.py`, `config/sglang.env` and
`config/model-manifest.json`. Compare the fingerprint with the last calibration report to see
whether one is due. A steady boot serves at warm's effective concurrency
(`MAX_RUNNING_REQUESTS`, default 128, with warm's own KV/OOM fallback to 64). Set
it to whatever the last calibration decided.

The numbered gates below are the calibration run. Steady runs gates 1 to 4,
then the tunnel without the public probe, then guards without the benchmark,
then handoff (with the guard health check) and key-leak.

## Supervised run

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

Set both `PROVISIONED_AT` (the original provisioning time, epoch seconds or ISO
with a timezone) and `CREDIT_CAP_SECONDS` (the run budget in seconds) before boot.
Neither has a default: the run window is a cost call made at launch, and boot
stops with a clear message if either is missing. The deadline is
`PROVISIONED_AT` + `CREDIT_CAP_SECONDS` − 600 seconds. An existing warm cutover bound reserves another 180 seconds. A
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
   require a pinned engine image and guard revisions; check disk; then stage
   both guards. On `RESUME` the main weights disk check is skipped, as before,
   but the guard disk check and staging still run. With the files already
   verified, the guard need is 0 and nothing downloads.
   `ALLOW_UNPINNED` cannot permit a launch.
   - **Disk:** the guard need is the size of every manifest file that is
     missing or doesn't verify under `GUARD_WEIGHTS_DIR`, plus 1 GB. It is 0
     when all files already verify. If the guards and `WEIGHTS_DIR` share a
     filesystem, the requirement is the weights need plus the guard need, checked
     once. Too little room for the guards always fails, before any download.
     `DISK_SPACE_ACTION=warn` relaxes only the main model's budget.
   - **Guard staging:** each guard goes into
     `GUARD_WEIGHTS_DIR/<repo>/<revision>/`. Every file is checked against
     `config/manifests/guards.json` (size and SHA256 for the pinned revision).
     A verified local copy is reused, so a rerun downloads nothing. Otherwise
     files download in parallel (`GUARD_WORKERS`, default 4) from the R2 mirror
     only, over S3, or from `$MIRROR_ORIGIN/models/<repo>/<revision>/` with
     `MIRROR_AUTH_HEADER` and no redirects when `MIRROR_HTTP=1`. The mirror
     origin is `MIRROR_URL`'s scheme and host. `MIRROR_ONLY=1` is the default
     for the guards too; `MIRROR_ONLY=0` is a debug flag that adds Hugging Face
     at the same revision as a last source. If no source verifies, preflight
     fails before warm.
     Each finished file is logged to `runs/node-run-<id>/guards.log`.
2. **warm-timeout:** use `WARM_TIMEOUT_SECONDS=3600` unless explicitly configured;
   cap it by the remaining absolute deadline, including warm's earlier bound.
3. **key:** read the piped key without prompting or echoing.
4. **warm:** foreground `warm.sh --layout tp8`; require one successful READY
   signal and matching pinned image. Its existing KV/OOM fallback may select 64.
   This path uses only `hello world` for warm smoke inputs and skips prefix replay.
5. **guards:** launch both detached guard containers from that same image,
   bound to loopback 8600/8601, only from the dirs preflight staged. Each file
   is checked again first. A marker written after hashing (SHA256 plus size,
   times and inode) makes this cheap. A file changed within a second of its
   marker is hashed again. Any edit since preflight forces a rehash,
   and a mismatch fails the gate before a container starts. The container mounts
   the staged dir read-only with `HF_HUB_OFFLINE=1` and `--served-model-name`
   set to the repo id, so nothing downloads at launch. Measure per-GPU memory
   before/after and collect compute-process memory when supported. Both guards
   must become healthy, then pass the guard benchmark (below). On `RESUME`, a
   guard container that is still running and healthy is kept and not rechecked;
   any guard that has to be relaunched is.
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
warm still verifies checksums. The guards are about 17 GB when nothing is
staged yet; preflight adds that need as described above. Allow separate space
for Docker layers.

`DRY_RUN=1 ./scripts/node-run.sh` prints the gate plan without reading node env or
stdin, probing hardware/network, or creating files. Test-only overrides require
`NODE_RUN_TESTING=1`: `EXPECT_GPU_NAME`, `EXPECT_GPU_COUNT`,
`NODE_RUN_WARM_CMD`, `NODE_RUN_RELAY_URL`, `NODE_RUN_GUARD_HEALTH_URL`,
`NODE_RUN_TUNNEL_PUBLIC_URL`, `NODE_RUN_ENGINE_HEALTH_URL`, `CLOUDFLARED_BIN`,
`CLOUDFLARED_LOG`, `CLOUDFLARED_GITHUB_URL`, `GUARD_MANIFEST`, `GUARD_HF_URL`. Do not set these on the paid node.

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

## Watching a run

From the Mac, while node-run is going:

```sh
ssh -t ubuntu@<node> '~/b300-inference/scripts/status.sh --loop'
```

Drop `--loop` for one screen. Add a number after it to change the redraw
interval (default 2 seconds). It needs no `TERM`, `watch` or `tput`, and only
clears the screen when it is writing to a terminal. It reads `nvidia-smi`, file
sizes under `WEIGHTS_DIR` and `GUARD_WEIGHTS_DIR`, and the run reports, and never
reads an env file. The ETA comes from the download rate between two samples.

Logs, all under `~/b300-inference/runs/`:

| File | Written by | When |
| --- | --- | --- |
| `node-run-<id>.json` | node-run | every gate, live |
| `node-run-<id>/guards.log` | guard staging | one line per finished guard file |
| `node-run-<id>/warm.log` | node-run | only after warm exits |
| `node-run-<id>/tunnel.log` | node-run | after the tunnel helper runs |
| `warm-<id>.json` | warm | every warm stage, live |
| `warm-<id>/weights.log` | model download | start line, then one line per finished file |
| `warm-<id>/operations.log` | warm | docker pull and other commands |
| `warm-<id>/engine.log` | warm | engine output, live |

There is no `rclone-prestage.log` in this flow. `runs/<id>/weights.log` from
`bootstrap.sh` is a different, older path. The model download reports per file,
so it is quiet while big shards stream; `status.sh` shows the bytes moving.

**Weights come straight from R2 over S3 by default.** Set `MIRROR_S3_ENV_FILE`
(for example `~/mirror-s3.env`, mode 600, owned by you) in `~/node.env`. `node-env.sh`
reads it as `KEY=VALUE` data, never sources it, and exports only
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL` (or `R2_ENDPOINT`),
`AWS_REGION`/`AWS_DEFAULT_REGION` and `R2_BUCKET`. With those set, every main model
file comes from `s3://$R2_BUCKET/models/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8/<revision>/<file>`
(no `dealignai/` segment) and every guard file from
`s3://$R2_BUCKET/models/<org>/<name>/<revision>/<file>`, using parallel ranged GETs
(boto3, 64 MB parts, `S3_CONCURRENCY` per file, default 16; `SHARD_WORKERS` files at
once). Each file is checked for size and SHA256 against the pinned manifest before it is
renamed into place, and verified markers make reruns skip it. `MIRROR_ONLY=1` is the
default for the main model: if S3 fails for a file, the download stops and nothing is
fetched from Hugging Face. `MIRROR_ONLY=0` is a debug flag that allows a loud
`S3 FAILED ... FALLING BACK TO HF` fallback at the same revision. `MIRROR_S3=0` disables S3. Preflight requires `boto3` when S3 is on.

The Cloudflare Worker HTTP mirror (`MIRROR_URL` + `MIRROR_AUTH_HEADER`) is **off by
default** for weights. Only `MIRROR_HTTP=1` turns it back on, after S3 and before
Hugging Face (only with `MIRROR_ONLY=0`) for the main model and the guards.

`MIRROR_URL` is the model base, `<origin>/models/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8`,
with no `dealignai/` org segment: shards are read from `<MIRROR_URL>/<revision>/<file>`
and the mirror manifest from `<MIRROR_URL>/<revision>/manifest.json`. The guards keep
the org: `<origin>/models/<org>/<name>/<revision>/`.

### mirror.env

The mirror bearer lives in one line, either `Bearer <token>` or
`Authorization: Bearer <token>` (a `MIRROR_AUTH_HEADER=` prefix is also accepted).
It has a space in it, so never `source` it: the shell runs `Bearer` as a command.
Set `MIRROR_ENV_FILE` (for example in `~/node.env`) and `node-env.sh` reads line 1
as data into `MIRROR_AUTH_HEADER`. The file must be mode 600 and owned by you.

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
late gates, `handoff` and `key-leak`. A resume keeps the run's mode; a steady
run can be finished as a calibration with `--calibrate`. They append to the same
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

## Engine crash after serving

`scripts/serve_group.py` runs inside the engine container and watches every
serve process. Once the whole plan has answered `GET /health`, a crash no longer
ends the night: it logs the exit codes to `engine.log`, stops the rest of the
plan, waits 15 seconds and starts it again in the same container, up to 3 times.
Each restart reloads weights from the local disk, about 10 minutes on a B300.
The gateway answers 503 meanwhile. A crash before the first healthy answer still
stops the container, so a bad image or flag fails warm-up instead of looping.
Guards, the tunnel and the relay are separate and keep running.

## Cleanup

A fresh run (not a resume) starts clean. After taking the run lock, preflight
removes every container named `b300-warm-*` or `node-run-*`, and stops any
`sse_relay.py` from this checkout run by the same user. It then checks ports
8000, 8001, 8600 and 8601. If one is still held, it fails and names the port,
for example `port 8000 (relay) is held by a process that is not ours`. The
report lists what was removed under `cleanup`.

## Guard benchmark (calibration only)

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
mocked. It runs a steady boot, then a calibration run. The calibration run fails at the tunnel, keeps the engine, and is resumed through
guards, the guard benchmark, relay-safety, loadtest, fit and tunnel. CI
(`.github/workflows/ci.yml`) runs lint, the unit tests and `make dry-run` on
each pull request. None of it proves B300 latency, model accuracy or that the
pinned image can serve the guards.

## Tunnel (required)

On the node, before running node-run.sh:

```sh
install -m 755 ~/b300-inference/vendor/infer-gateway/node-tunnel.sh ~/node-tunnel.sh
```

`~/node.env` (mode 600) must define a non-empty `CF_TUNNEL_TOKEN`. Set it through a masked card; never paste the value in chat. node-run.sh sources it like Netops does (`set -a; . ~/node.env; set +a`) without echo, and passes the token only to `~/node-tunnel.sh`.

Preflight hard-fails before any download or docker pull if `~/node-tunnel.sh` is missing or not executable (the message prints the `install` line above), `CF_TUNNEL_TOKEN` is missing or empty, `sha256sum` is missing, or no verified cloudflared can be staged.

**cloudflared.** Preflight puts cloudflared 2026.10.7 at `/tmp/cloudflared` and checks its sha256 against the pin in `scripts/node_tunnel.py` (`CLOUDFLARED_SHA256` overrides it). It reuses a binary that already matches. Otherwise it downloads `tools/cloudflared/2026.10.7/cloudflared-linux-amd64` from the origin of `MIRROR_URL` (its scheme and host; `MIRROR_URL` itself is the model base) with the same `MIRROR_AUTH_HEADER` as the model download (no redirects followed), and only if that fails or does not match, the GitHub release of the same version. A file that does not match is deleted and never run. Netops' helper then sees a binary that matches its own pin and skips its download.

**Readiness.** After fit, the tunnel gate runs the helper (60 s cap), then waits up to `TUNNEL_CONNECT_TIMEOUT` (180 s) for `Registered tunnel connection` in the part of `/tmp/cloudflared.log` written after launch, failing early if cloudflared exits. Then it probes the public URL without credentials: a `POST /v1/safety/csam` that the relay answers with 401. It prints `TUNNEL UP url=...` or fails the gate with the reason (`tunnel helper failed`, `helper timeout`, `cloudflared exited before registering a connection`, `registered no edge connection`, or `connected but the public URL never reached the relay`). A tunnel failure keeps the engine and prints the RESUME command. `/tmp/cloudflared.log` is raw and unredacted; do not share it.

**Why it said `TUNNEL NOT STARTED reason=timeout` before.** That one reason covered three different cases: no time left before the deadline, a helper that ran past the wait, and a launched tunnel whose public probe never passed. The probe only accepted a relay 401 on `/v1/safety/csam`. With `PRIVATE_UNGUARDED=1` the relay runs without SAFETY and has no such route, so the request passed through to the engine, which does not answer with the relay's `{"error": "unauthorized"}` 401. A working tunnel would still report `timeout`. The 180 s wait also included the helper's cloudflared download from GitHub. Now the download happens in preflight, readiness comes from cloudflared's own log, and each case has its own reason. The unguarded mode is gone, so the probe always goes to the relay's own route.

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

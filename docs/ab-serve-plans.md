# Noon A/B serve plans — November 1

This is an operator runbook for belweave's prepared eight-B300 node. No node,
image, GPU or tunnel was started to develop it. The default model remains
`dealignai/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8` at
`c19ca5ca4eaeb79d29fdf08aea8327da95b1116a`, with the manifest's base revision
`1bae0e1088993b9b82de08f59de5be5a34e42170`.

## Selectors

CLI values override their environment counterpart. Defaults are explicit and
independent of how much host memory happens to be available.

| CLI | Environment | Values / default |
|---|---|---|
| `--layout` | `LAYOUT` | **tp8**, dep4x2, dep8x1, dep2x4 |
| `--engram` | `ENGRAM` | **gpu**, host |
| `--dspark` | `DSPARK` | **off**, on |
| `--image` | `ENGINE_IMAGE` | `lmsysorg/sglang:dev-dsv41`; `BAKED_IMAGE` takes precedence unless CLI image is supplied |
| `--image-digest` | `IMAGE_DIGEST` | Optional `sha256:` plus 64 lowercase hex digits; no default digest |
| `--print-plan` / `--dry-run` | `DRY_RUN=1` (warm/launch) | Render only; no Docker, network, credentials or hardware access |
| `--available-ram-gb` | none | Decimal GB assumption for host-mode dry-runs only; rejected for live launches |

`warm.sh`, `measure.py`, and `bootstrap.sh --layout ...` accept these flags.
A/B flags or selector environment variables make bootstrap enter the **node-only
warm path before reading node.env**. It does not run legacy bootstrap networking,
gateway or teardown steps. Use `warm.sh` directly for the clearest lifecycle.
With no selectors, legacy bootstrap retains its existing behavior.
`ATTN_MODE=dp_ep` remains a warm alias for `dep8x1`; explicit `LAYOUT` wins.
`SPEC_DECODE=mtp` stays rejected; select DSpark with `DSPARK=on`.

| Layout | Engines | GPU devices | HTTP ports | Distributed init ports | Front door |
|---|---|---|---|---|---|
| tp8 | TP8/EP8 | 0–7 | 8000 | engine default | engine :8000 |
| dep4x2 | 2 × TP4/EP4/attention-DP4 | 0–3; 4–7 | 8100; 8200 | 29500; 29600 | router :8000 |
| dep8x1 | 1 × TP8/EP8/attention-DP8 | 0–7 | 8100 | 29500 | router :8000 |
| dep2x4 | 4 × TP2/EP2/attention-DP2 | 0–1; 2–3; 4–5; 6–7 | 8100; 8200; 8300; 8400 | 29500; 29600; 29700; 29800 | router :8000 |

All HTTP listeners and explicit distributed-init addresses use `127.0.0.1`.
One Docker container supervises all processes; each engine gets its own
`CUDA_VISIBLE_DEVICES`. DEP uses `sglang_router.launch_router --policy cache_aware
--dp-aware`: prefix affinity with load balancing, not a promise of strict session
hashing. Pure consistent hashing was not selected because its fallback may hash
the shared Authorization header, concentrating requests on one worker.
The router executable must already be present in the selected engine image.
There is no second unpinned router image or installation during launch.

DEP sets `--tp-size N --ep-size N --attn-dp-size N --enable-dp-lm-head`.
It does not combine independent replicas via `--dp-size`, or emit the deprecated
`--enable-dp-attention`. Every replica loads a full model partitioned across its
own GPUs; total model memory multiplies by replica count. DEP2x4 is a rendering
option, **not a claim that this checkpoint/context fits two GPUs**.

GPU Engram renders `--engram-config '{"placement":"gpu"}'`; host renders
`'{"placement":"cpu"}'`. Host mode requires measured `MemAvailable >=
203 GB × replicas + 32 GB`. That is 235 GB for tp8/dep8x1, 438 GB for dep4x2,
and 844 GB for dep2x4. The check runs before downloads, after downloads, and again
inside the container. Failure stops launch; it never silently changes to GPU mode.
`ENGRAM_TABLE_GB` and `ENGRAM_HEADROOM_GB` may raise these floors, not lower them.
These are minimum gates, not a proof of fit when HiCache or other processes use RAM.
The deprecated `SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE` is neither passed nor set;
the supervisor removes inherited image values. There is no legacy fallback.

DSpark on adds `--speculative-algorithm DSPARK --speculative-dspark-block-size 5`.
MTP/EAGLE are not substituted. Both layouts retain the explicit `deepseek-v41`
reasoning parser, `deepseekv41` tool parser and `--trust-remote-code`.

## Render the matrix without a node

Install the repo's test/runtime dependencies into a local venv if needed. No
credential variables, deadline or tunnel slot are required for these dry-runs.

```bash
for layout in tp8 dep4x2 dep8x1 dep2x4; do
  for engram in gpu host; do
    for dspark in off on; do
      .venv/bin/python scripts/measure.py --print-plan \
        --layout "$layout" --engram "$engram" --dspark "$dspark" \
        --available-ram-gb 900
    done
  done
done

# Identical plan builders through the launch entrypoints:
PATH="$PWD/.venv/bin:$PATH" ./scripts/warm.sh --print-plan --layout dep4x2 --engram gpu
PATH="$PWD/.venv/bin:$PATH" ./bootstrap.sh --print-plan --layout tp8 --engram host --available-ram-gb 235
```

JSON includes the model revision, variant, image ref/digest/tag, Docker create/start
argv, each engine/router argv, per-process environment and shell-quoted renderings.
API-key values are deliberately absent: the runtime adds `--api-key` to each
process from the existing key delivered over Docker stdin. Never save that stdin
payload. Rendered Docker commands require that payload; they are inspection
artifacts, not standalone unauthenticated launches.

To pin an image, supply a **real, qualified** digest in `IMAGE_DIGEST` or use
`ENGINE_IMAGE=repository@sha256:...`. The ellipsis is documentation, not a valid
value. No digest was invented or selected here. Without one, warm, dry-run and
measurement warn and record the mutable tag plus the actual loaded image ID
when available. A supplied digest is used for pull/inspect; launch uses the
resolved immutable local image ID.

## Run one factor at a time on the prepared node

Only run these during the separately authorized noon session, on a node whose
port 8000 is free. Keep any cutover watcher disabled during experimental warm-ups:
each successful warm emits the normal ready signal. An existing live engine is
never replaced automatically.

The operator must already have exported the existing `INFER_UPSTREAM_KEY`,
`WINDOW_END` (old-node epoch), and `TUNNEL_SLOT=a` or `b`. Warm does not source
credential files. Optional `MIRROR_AUTH_HEADER`, mirror URL and base weights path
retain their existing behavior. Leave the key, model, image digest, workload,
context, graph sizes, memory fraction and running-request limit unchanged across
paired runs.

```bash
# Baseline:
./scripts/warm.sh --layout tp8 --engram gpu --dspark off

# Alternatives, ONE at a time after measuring/stopping only your preceding test:
./scripts/warm.sh --layout dep4x2 --engram gpu --dspark off
./scripts/warm.sh --layout tp8 --engram host --dspark off
./scripts/warm.sh --layout tp8 --engram gpu --dspark on

# Equivalent environment selection:
LAYOUT=dep8x1 ENGRAM=gpu DSPARK=off ./scripts/warm.sh
```

Warm records exact commands and image identity in `runs/warm-<id>.json`, prints
that path as `timing_file` in `READY`, and saves merged, redacted worker/router
logs at `runs/warm-<id>/engine.log`. All processes share one container name in
that same record. A child exit stops the plan. Readiness requires public `/health`,
401 on unauthenticated `/v1/models`, authenticated model listing for **every**
worker and router, then nonstreaming and streaming completions through port 8000.
A ready signal preserves schema 1 and the selected `tunnel_slot`. No tunnel or
gateway reconfiguration is needed: port 8000 is still the front door.

Clients use `https://api.infer.belweave.ai/v1`; see the
[gateway contract](cutover.md#gateway-contract) for the migration alias.

```bash
# Fill in the timing_file path printed by this run, not an older warm:
warm_report=runs/warm-REPLACE_WITH_THIS_RUN.json
python3 scripts/measure.py --warm-report "$warm_report" \
  --log "${warm_report%.json}/engine.log" \
  --layout tp8 --engram gpu --dspark off --output runs/noon-tp8-gpu-off

# For the DEP run, labels must match its own warm report:
python3 scripts/measure.py --warm-report "$warm_report" \
  --log "${warm_report%.json}/engine.log" \
  --layout dep4x2 --engram gpu --dspark off --output runs/noon-dep4x2-gpu-off

# Stop only the disposable test container named by that report before the next warm.
# This does not select or stop an unrelated live container.
container=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["container"])' "$warm_report")
docker stop "$container"

python3 scripts/measure.py --compare \
  runs/noon-tp8-gpu-off.json runs/noon-dep4x2-gpu-off.json \
  --output runs/noon-layout-comparison
```

Measurement never launches/restarts a server. It verifies supplied variant/image
labels against the warm record and checks container image, running state and
start time; a restart invalidates the record. Omitting labels adopts the recorded
variant. Old reports without a launch plan require a fresh warm. JSON results
include the full plan and resolved image identity. Pairwise comparison permits
exactly one of layout, Engram or DSpark to change and rejects other engine,
model, workload or image-ID changes.

Concurrency remains KV-pool bounded: the report uses the **minimum** logged
`max_total_num_tokens` across ranks/replicas, never an assumed sum, with 10%
headroom. `MAX_RUNNING_REQUESTS` is per engine/rank and not a guaranteed aggregate
capacity. Missing KV/cache metrics stay unknown. The fixed-prefix sweep measures
reuse; it does not prove traffic distribution or throughput on diverse sessions.

## TODO for noon: qualify the actual image

- Select and record the real digest. Source SHA is not an image digest.
- Confirm the image contains `sglang_router`, its `cache_aware`/`--dp-aware`
  options, and the current `--attn-dp-size`/`--enable-dp-lm-head` options.
- Confirm `--engram-config` and the `EngramConfig.from_dict` validation API are
  present. The exact GPU/CPU schema is source-confirmed, but the preview tag may
  lag it. Warm fails closed on missing options/imports/invalid syntax; no guessed
  compatibility fallback is enabled.
- Confirm DSPARK/block-size-5 with the pinned **uncensored** checkpoint, especially
  with DP attention and host Engram. Source support does not establish numerical
  correctness or a latency win on this revision.
- Confirm per-replica weight/KV memory fit, huge-page/host-memory requirements,
  loading peak RAM, router authentication propagation and prefix locality on B300.
  DEP2x4 may not fit. Runtime smoke and measured pool sizes are mandatory.

[Source evidence](sglang-warm-flags.md) pins the researched flags. No actual image
was pulled, executed or qualified for this change.

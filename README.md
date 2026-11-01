# b300-inference

A single-node inference kit for a four-hour, 8× NVIDIA B300 rental. A Mac watches
one national compute grid account, accepts a normalized SSH claim, uploads the
kit, and retrieves benchmark artifacts. No provider integration, purchase,
tunnel connection, remote repository, or deployment has been performed.

The first window is a test. Capacity, latency, cold start, chat correctness, and
handoff are **not yet measured on B300**. Image/model support below is based on
upstream documentation inspected 2026-10-31, not a completed GPU qualification.

## Quick start

```bash
make lint                         # shellcheck must be installed; brew install shellcheck
python3 -m venv .venv
.venv/bin/pip install 'aiohttp>=3.12,<4'
PATH="$PWD/.venv/bin:$PATH" make test
DRY_RUN=1 ./bootstrap.sh
DRY_RUN=1 ./watcher/watch.sh --once
```

Before a real rental, prepare Linux x86_64 with Docker, NVIDIA Container Toolkit,
Python ≥3.10, `requirements-node.txt`, `rsync`, `curl`, `flock`, `setsid`, `timeout`,
and optionally `s5cmd`. The kit checks Docker/toolkit rather than installing or
restarting the driver stack during a paid window. Its CUDA-13 engine images need
a driver ≥580; the preflight verifies eight B300s, SM 10.3 in PyTorch, NVLink,
CUDA capability, and weight-disk space. Reserve another 100 GB on Docker's data
filesystem for image layers; the weight check defaults to 650 GB minus existing
shards, with at least 20 GB free. These are planning allowances, not measured peaks.

1. Supply a sanitized portal reply to finish `claim/parse.py`. The fixture is
   deliberately blank of provider syntax. Hooks currently return “not configured”.
2. Copy `config/account.env.example` to `config/account.env`; it holds one account
   name and a hook path only. Credentials come from the environment or a future
   Keychain hook. **No account lists, rotation, or multi-account automation exist.**
3. Export secrets through your credential manager. Do not fill secret values into
   `.env.example`, account config, shell scripts, plist, command text, or any file.
   `.env.example` lists names only and is not a configuration to source wholesale.
4. Trust the assigned host's SSH host key through an independently verified
   channel. SSH uses strict host checking and an existing identity path from env;
   the kit does not create or copy private keys. Set `NODE_SSH_KEY_PATH`.
5. For an already normalized claim: `./bootstrap.sh --claim /path/to/claim.json`.
   Otherwise use `./watcher/watch.sh`. `--once` checks once; an assigned window
   completes before it exits. Dry run always uses stubs, even if custom hooks exist.
6. Inspect `runs/<id>/remote.status.json`, then `runs/<id>.tar.gz`. The first
   benchmark is reserved atomically by `runs/first-benchmark.lock`. Remove that
   directory manually to retry a failed first benchmark; do not assume it passed.

`bootstrap.sh` without `--claim` runs **on the node**. Reuse its `RUN_ID` and original
`WINDOW_END` for an idempotent retry. It reuses running containers, verifies weights,
and does not extend the rental deadline. A stopped run needs a new ID. Manual
teardown: `RUN_ID=<id> ./teardown.sh`, with the upstream key exported for metrics.
An independent node-local timer survives SSH disconnect and begins teardown at
`WINDOW_END - 600`; a reboot does not preserve that timer. Provider expiry remains
the ultimate cutoff. This kit stops workloads; it does not release a billable
allocation or control the grid's billing clock.

The launchd plist is a template only: edit absolute paths, create `logs/`, and
arrange runtime credentials through a credential-manager wrapper before installing
it yourself. Do not put secrets in launchd `EnvironmentVariables`.

## Network contract and handoff

```text
Clients -> https://infer.belweave.tech
           Cloudflare Worker: keys, per-key limits, between-window 503
                         |
           infer-origin.belweave.tech / named tunnel b300-infer
                         |
           outbound cloudflared replica(s), same token each window
                         |
           127.0.0.1:8000  SGLang or vLLM, upstream API key required
                         |
                     8 × B300

Mac -> grid portal claim hook -> SSH bootstrap -> status/benchmark/archive
Optional local LiteLLM at 127.0.0.1:4000 -> authenticated engine :8000
```

The engine binds loopback; there are no Docker published ports. Linux host
networking allows cloudflared to reach loopback. A host/provider ingress firewall
must also deny unsolicited connections to distributed-worker/NCCL ports, which
are internal engine implementation details. The API key is supplied to the engine's
`--api-key` flag from `INFER_UPSTREAM_KEY`, with `ORIGIN_KEY` as fallback. It travels
through an anonymous SSH/Docker stdin pipe. Docker logging is disabled; attached
output is redacted before being written. Never use `set -x`, `docker inspect` of
secret-bearing containers from other tooling, or environment dumps in artifacts.

Netops' script is kept verbatim at `vendor/infer-gateway/node-tunnel.sh`. Bootstrap
calls it **after authenticated health + a real chat completion**, maps
`CF_TUNNEL_TOKEN` to `TUNNEL_TOKEN` in that child only, and extracts its printed PID.
A new node joins the same tunnel while the old node remains connected. The script's
successful start/PID is not proof of edge routing or a zero-downtime handoff: Netops
must verify tunnel registration, Worker routing, upstream key agreement, origin
access controls, request timeouts, and streaming behavior.

Teardown collects logs, GPU dumps, metrics, and benchmark files first. It supports
an optional bounded `DRAIN_HOOK` executable for a version-specific engine admission
stop. There is no universally verified drain endpoint for these two builds, so
none is fabricated. It polls running **and queued** request gauges for three zero
samples, for at most `DRAIN_SECONDS` (120 by default). Missing gauges mean “unknown”,
never zero. If arrivals continue or metrics are absent, the bound may expire and
remaining requests will be cut off.

* Default (`REPLACEMENT_READY=0`): drain as above, stop gateway/engine, then signal
  cloudflared immediately, in the EXIT cleanup path as well. This follows Netops'
  connector-last contract, but has a short dead-origin routing race and cannot
  guarantee admission stops without a tested drain hook.
* Confirmed replacement (`REPLACEMENT_READY=1`): disconnect the old connector
  **first**, then bounded-drain and stop services. Set this only after Netops confirms
  another replica is healthy. The watcher does not infer readiness from a PID.
  cloudflared's own graceful-shutdown limit may be shorter than DRAIN_SECONDS;
  test long streams before promising seamless handoff.

PID cleanup verifies the executable name, with a narrowly scoped
`pkill -f '^/tmp/cloudflared tunnel '` fallback on this dedicated node. The
vendored helper has known issues: unpinned **latest**, no checksum, linux-amd64
only, a token in process argv, and raw `/tmp/cloudflared.log`. Those are upstream
exceptions to the kit's otherwise env-only secret transport; the helper is not
modified. Archive copies are redacted. Ask Netops for an architecture-aware pinned
binary, verified checksum, env-token use without argv, sanitized logs, explicit
shutdown settings, and consistent env names. The kit rejects ARM nodes with the
Cloudflare path rather than silently downloading the wrong binary.

`GATEWAY=litellm` creates an optional authenticated loopback gateway; you must pin
`LITELLM_IMAGE`. The Netops tunnel still targets the engine at :8000, so enabling
LiteLLM does **not** move it into the public path. That would require a revised
Netops port/ingress contract. `TUNNEL=none` is local-only. `TUNNEL=tailscale` fails
with a clear hook-required message: Funnel would be a separate public front door
and needs a separately reviewed adapter. No automatic Funnel command is shipped.

## Model facts

The [1017 card](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017), raw
config, file metadata, and full LICENSE were reachable. The release was posted
October 17, 2026; the API reports no gate and 83 files. The pinned revision is
`1bae0e1088993b9b82de08f59de5be5a34e42170`.

| Property | Finding |
|---|---|
| Architecture | `DeepseekV41ForCausalLM`, `deepseek_v41`; multimodal MoE with vision tower; CED 20 encoder + 20 decoder layers; CSA2 compressed sparse attention and sliding windows, not a generic V3 MLA recipe |
| Parameters | Family card: 552B backbone + 196B Engram conditional memory = 748B, before separately accounting for vision/draft components. Exact all-component logical total: TBD. HF safetensors metadata reports 763,205,315,794 entries, including packed I8; that is **not** an unqualified logical parameter count |
| Active parameters | Family card/engine docs: 8B per token in prefill, 16B in decode. These are architecture-family figures, not independently measured on 1017; the 1017 card itself does not state them |
| Experts | 384 routed + 1 shared; 6 routed experts per token |
| Attention/context | CSA2, 128-token sliding window; 1,048,576 max positions with YaRN; native FP4 main KV described by family card |
| Draft layers | `num_nextn_predict_layers=3`; DSpark block size 5. Do not equate the config's nextn label with generic engine MTP/EAGLE support |
| Base precision | FP8 dynamic, 32×32 weight blocks, ue8m0 scales, FP4 experts; some BF16/F32 tensors. `dtype=bfloat16` does not mean a full BF16 checkpoint |
| Base size | 510,296,708,312 bytes safetensors (510.297 decimal GB); all listed files 510,311,045,295 bytes (510.311 GB), roughly 475.26 GiB |
| Official NVFP4 | `nvidia/DeepSeek-V4.1-Flash-1017-NVFP4`, released Oct 30; 428,986,460,424 weight bytes / 429,008,964,498 total bytes. Not silently substituted for the requested DeepSeek checkpoint |
| Other formats | No separate official full-BF16 or uniform-FP8 1017 checkpoint verified. Base is already mixed precision; conversion is not equivalent to a separately released checkpoint |
| Chat encoding | No Jinja chat template. DeepSeek's `encoding/encoding.py` / deepseek-recipe supplies model-specific formatting. SGLang's V4.1 preview has native encoding and `auto` parsers. vLLM v0.32.0 registers `DeepseekV41ForCausalLM`; use native `deepseek_v41` tokenizer/reasoning/tool modes. Real chat smoke is mandatory |

**License:** MIT applies to repository and weights. Commercial serving and charging
for API access are permitted by that license; there is no resale ban or
acceptable-use rider in the inspected LICENSE. Preserve the copyright and
permission notice in copies/substantial portions; retain the warranty disclaimer.
No API-output branding requirement is stated. This conclusion does not grant
permission to resell subsidized grid resources: grid terms, account eligibility,
who pays, and billing/resale authorization remain separate unresolved questions.

## Serving choice

Default: **SGLang `lmsysorg/sglang:dev-dsv41`**, the model cookbook's NVIDIA preview
for H200/B200/B300/GB300. The cookbook explicitly says stock releases do not yet
ship V4.1 support; a generic latest/stable tag is therefore not a safe substitute.
The CUDA-13 installation lane and model recipe support SM100/SM103. Bootstrap
records the actual image digest; pin `ENGINE_IMAGE=...@sha256:...` after the first
qualified run because `dev-dsv41` can move.

Fallback: **vLLM `vllm/vllm-openai:v0.32.0`**, the upstream release's CUDA-13.0 image;
`vllm/vllm-openai:v0.32.0-cu129` is an explicitly published CUDA-12.9 alternative,
not this kit's default. The v0.32.0 registry contains the V4.1 class. Exact B300
throughput and every flag combination still require the rental test. There is no
automatic engine switch on failure, which would hide the cause and waste time.

TensorRT-LLM is not chosen: no equally concrete 1017/B300 launch recipe was verified
in this research. NVIDIA's model-specific NIM guide instead uses its **SGLang**
experimental image `nvcr.io/nim/nvidia/sglang-model-free-nim-experimental:2.1.6-variant-deepseek-v4.1-flash-1017`
with a matching recipe; that is not evidence that TensorRT-LLM itself supports it.

The kit's baseline is a conservative experiment, not a claim of optimal settings:

| Setting | SGLang | vLLM |
|---|---|---|
| Parallelism | `--tp 8` | `--tensor-parallel-size 8` |
| Capacity | `--max-running-requests 256` | `--max-num-seqs 256` |
| Prefill | `--chunked-prefill-size 4096` | `--enable-chunked-prefill --max-num-batched-tokens 4096` |
| Graph ceiling | `--cuda-graph-max-bs-decode 256` | `--compilation-config '{"max_cudagraph_capture_size":256}'` |
| Memory/context | `--mem-fraction-static 0.80 --context-length 32768` | `--gpu-memory-utilization 0.80 --max-model-len 32768` |
| Auth/network | `--host 127.0.0.1 --port 8000 --api-key` from env | same |
| Native chat | `--reasoning-parser auto --tool-call-parser auto` | `--tokenizer-mode deepseek_v41 --reasoning-parser deepseek_v41 --tool-call-parser deepseek_v41 --enable-auto-tool-choice` |

Keep KV dtype and kernel selection automatic: forcing FP8 KV would override this
model's native FP4 design. Do not force an old FlashAttention/DeepGEMM backend.
Leave speculation off for high concurrency. Compare `SPECULATION=dspark` for
interactive latency (SGLang `--speculative-algorithm DSPARK
--speculative-dspark-block-size 5`; vLLM speculative JSON method `dspark`, 5 tokens).
The SGLang cookbook explicitly excludes EAGLE/generic MTP for this model.

After baseline success, compare SGLang `PARALLELISM=dp_ep` (`--attn-dp-size 8
--ep-size 8` with TP8). This is an experimental local topology, not a verified
copy of an eight-B300 high-throughput cell. DP attention can avoid attention TP
collectives while EP redistributes experts, but adds routing/collective costs and
changes cache placement. Keep the better measured TTFT/TPOT result. vLLM dp_ep is
intentionally rejected until its topology is separately configured. Sweep graph
ceilings and capacity together; 1024 client streams may queue behind a 256-request
engine cap. A 1M architectural context does not mean 1024 simultaneous 1M prompts
fit. These defaults cap requests at 32K.

## Weight delivery and window budget

Preference: **already staged local persistent NVMe volume**, then **same-region
S3/R2 mirror**, then **HF Xet direct**. A remote persistent network volume may be
slower than a local download. Hash every file against the pinned HF SHA256 (large
files) or Git blob SHA1 (small files); the pinned manifest is retained on the volume.
A volume run needs no HF request. S3/HF obtain authoritative manifest metadata
from HF; an S3 mirror must preserve the pinned repo layout and tokenizer/encoding
files, not just weight shards. No mirror or volume is created by this kit.

HF uses `huggingface_hub.snapshot_download(max_workers=16)` (equivalent concurrency
control to `hf download --max-workers 16`) and `HF_XET_HIGH_PERFORMANCE=1`. Current
Hub uses `hf_xet`; legacy `HF_HUB_ENABLE_HF_TRANSFER`/`hf_transfer` is deprecated.
Downloads resume through the HF local cache; corrupt completed shards are discarded
and fetched again. S3 uses four file workers × eight multipart workers by default;
verified shards are skipped on retries, but an interrupted individual S3 shard
restarts. `s5cmd` is chosen over rclone for a small S3-native parallel copier;
rclone is a valid alternative for other storage backends, not implemented here.

For size S decimal GB and link R Gbit/s, ideal minutes = **8S / (60R)**.
Planning transfer time below assumes 70% useful link utilization, excluding
hashing, image pull contention, decompression, cache misses, and load/compile.

| Link | 510.311 GB ideal | At 70% utilization | 429.009 GB NVFP4 ideal |
|---|---:|---:|---:|
| 1 Gbit/s | 68.04 min | 97.20 min | 57.20 min |
| 10 Gbit/s | 6.80 min | 9.72 min | 5.72 min |
| 25 Gbit/s | 2.72 min | 3.89 min | 2.29 min |

**Unmeasured cold-start planning allowances:** persistent local volume 15–35 min;
same-region mirror at sustained 10–25 Gbit/s 20–50 min; HF at sustained 10–25 Gbit/s
25–60 min; at 1 Gbit/s allow roughly 110–140 min. These are budgets, not provider
promises. Xet/CDN throughput can be lower. A full 510 GB hash pass alone takes
about 1.7–8.5 min at 5–1 GB/s disk read throughput. Record real phase times before
committing to an operating margin. Container pull and weight pull run concurrently,
so their wall-time contribution is approximately their maximum, not their sum.

| Phase | Budget (unmeasured) |
|---|---|
| Portal allocation / SSH | 2–5 min; use portal expiry, never restart its clock |
| Preflight + runtime checks | 1–2 min |
| Container pull | 3–10 min, overlapped with weights |
| Weight pull | table above; volume has no network transfer |
| Checksum pass | 2–9 min on suitable local storage |
| Load / compile / CUDA graphs | 8–20 min |
| Health / chat / warmup / tunnel | 1–3 min |
| First-window sweep | reserve 15–35 min, workload dependent; not a promise |
| Serving | remaining time to T−10 min |
| Drain / package / retrieval | final 10 min; timer starts on node even if Mac sleeps |

The load test reports TTFT p50/p95, p95 of **per-request mean TPOT**, per-stream
median decode tok/s, aggregate input/output tok/s, and errors at each concurrency.
It requires streamed usage token counts and DONE; it does not mistake SSE events
for individual tokens. First arrival can contain several speculative tokens, so
TPOT is a client-observed approximation, not a kernel-level inter-token percentile.
Unique prompt prefixes reduce shared-cache artifacts. The test records all streams,
persists after every step, and reports the maximum **tested** passing concurrency;
this is not an open-loop SLA or a proof of absolute maximum capacity. Benchmark
both direct loopback and the public Worker separately before accepting customers.

## Cost and revenue

Assumed price: $4/GPU-hour × 8 = **$32/hour**, **$128 per four-hour window**.
This is a scenario supplied for planning, not a grid quotation. Throughput =
**PLACEHOLDER until benchmarked**. No benchmark revenue is assumed.

Let C = cold-start minutes, B = benchmark/nonbillable minutes (0 for an ordinary
serving window). Sellable seconds **T = max(0, 240 − C − B − 10) × 60**.
With observed aggregate input I tok/s, output O tok/s, and respective prices
Pi and Po in dollars per million tokens:

```text
revenue = T × (I × Pi + O × Po) / 1,000,000
profit  = revenue − 128 − storage − egress − Worker/tunnel/gateway costs
output break-even O = (128 × 1,000,000 / T − I × Pi) / Po
if I = r × O: O_break_even = 128 × 1,000,000 / (T × (Po + r × Pi))
```

Example only: C=30, B=0 → 200 serving minutes (12,000 seconds). At zero input
revenue, the output throughput needed just to cover $128 is:

| Output price / 1M tokens | Break-even output tok/s |
|---:|---:|
| $0.25 | 42,666.7 |
| $0.50 | 21,333.3 |
| $1.00 | 10,666.7 |
| $2.00 | 5,333.3 |
| $5.00 | 2,133.3 |

For example r=4 input tokens/output token, Pi=$0.10, Po=$1.00 gives 7,619.0
output tok/s to break even under the same time assumption. This is arithmetic,
not measured capacity. If 25 min of the first window is benchmark-only, T falls
to 10,500 seconds. Billable output/reasoning and cache-hit input accounting must
match the Worker/billing contract; never treat raw engine token counters as revenue.

## Environment reference

All secrets are caller-exported only. The scripts do not automatically source
`.env`; an empty name in `.env.example` is not an instruction to create a secret file.

| Names | Default / purpose |
|---|---|
| `HF_TOKEN` | Optional for public model; env-only HF access |
| `CF_TUNNEL_TOKEN` → child `TUNNEL_TOKEN` | Required for default Cloudflare path; same named tunnel across windows |
| `INFER_UPSTREAM_KEY`, `ORIGIN_KEY` | Required origin key; first name wins |
| `LITELLM_MASTER_KEY`, `LITELLM_IMAGE` | Optional gateway key and explicitly pinned image |
| `NODE_SSH_KEY_PATH`, `NODE_KIT_DIR` | Existing SSH identity path; remote relative kit path `b300-inference` |
| `PROVIDER_API_TOKEN` | Reserved hook credential; no provider calls implemented |
| `ACCOUNT_CONFIG`, `ACCOUNT_NAME`, `PROVIDER_HOOKS` | One nonsensitive account config; default config/account.env |
| `MODEL`, `MODEL_REVISION` | 1017 model and immutable HF commit above; change both for a different checkpoint |
| `ENGINE`, `ENGINE_IMAGE` | sglang or vllm; image defaults in config/ |
| `WEIGHTS_SOURCE`, `WEIGHTS_DIR` | hf / s3 / volume; /data/weights/DeepSeek-V4.1-Flash-1017 |
| `WEIGHTS_S3_URI`, `S3_ENDPOINT_URL` | Existing mirror prefix; optional R2/S3-compatible endpoint; no credentials in URLs |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION` | Environment credentials for s5cmd; no credential files shipped |
| `ARTIFACT_S3_URI` | Optional artifact upload prefix; 90-second upload bound, local archive retained on failure |
| `RUN_ID`, `WINDOW_END`, `WINDOW_MINUTES` | Run ID; actual expiry epoch preferred; fallback 240 min from node bootstrap |
| `PORT`, `GATEWAY_PORT`, `GATEWAY`, `TUNNEL` | 8000 (required Netops contract), 4000, none, cloudflare |
| `DRAIN_SECONDS`, `DRAIN_HOOK`, `REPLACEMENT_READY` | 120; optional executable; 0 unless replacement independently verified |
| `FIRST_WINDOW` | 1; benchmark reserved once by local atomic marker |
| `TARGET_TTFT_MS`, `TARGET_TPOT_MS` | 2000, 50; illustrative targets, not promised SLA |
| `CONCURRENCY` | 16,32,64,128,256,512,1024 |
| `OUTPUT_TOKENS`, `PROMPT_REPEATS`, `REQUESTS_PER_STREAM`, `REQUEST_TIMEOUT_SECONDS` | 256, 64, 2, 180; synthetic workload controls |
| `MAX_RUNNING_REQUESTS`, `CHUNKED_PREFILL_SIZE`, `CUDA_GRAPH_MAX_BS` | 256, 4096, 256 |
| `MEM_FRACTION`, `CONTEXT_LENGTH`, `PARALLELISM`, `SPECULATION` | .80, 32768, tp / dp_ep, none / dspark |
| `ENGINE_EXTRA_ARGS_JSON` | JSON argv string array, no shell evaluation; do not include secrets |
| `MIN_FREE_GB`, `HEALTH_TIMEOUT_SECONDS` | 650 budget minus existing shards; 2400 seconds, also bounded by window |
| `HF_MAX_WORKERS`, `HF_XET_HIGH_PERFORMANCE`, `HASH_WORKERS` | 16, 1, 4 |
| `S3_WORKERS`, `S3_FILE_WORKERS`, `S3_PART_CONCURRENCY` | 32, 4, 8; bounded parallelism |
| `WATCH_INTERVAL_SECONDS`, `POLL_SECONDS`, `DRY_RUN` | 60, 20, 0 |

## Files and local verification

* `bootstrap.sh`, `loadtest.sh`, `teardown.sh`: node lifecycle and Mac `--claim` entry.
* `scripts/`: atomic status, checkpoint verification, argv generation, in-memory
  secret transport, authenticated HTTP, smoke, drain/deadline, redaction, benchmark, lint.
* `config/`: two engine presets, optional LiteLLM config, one-account template.
* `claim/`: parser stub, normalized claim validator, fixture placeholder, contract.
* `watcher/`: hooks, single-account loop, SSH deployment, runtime payload, notifications,
  detached benchmark launcher, and uninstalled launchd template.
* `vendor/infer-gateway/`: verbatim Netops script and provenance note.
* `tests/`: claim validation, offline dry runs, native flags/binding, secret redaction,
  status history, and a loopback fragmented-SSE test.
* `docs/model-metadata.json`: public HF metadata snapshot; `docs/planning.html`: transfer/cost chart.
* `.env.example`, `.gitignore`, `requirements-node.txt`, `Makefile`, this README.

`make lint` runs shellcheck and bash -n on every shell script, including vendor,
and py_compile on Python files. Tests use no GPU/provider/tunnel. `.venv`, logs,
runs, archives, secret env files, and generated status are ignored. No secrets
are stored by the kit's configuration or SSH/Docker transport. The exact vendor
script's argv/raw-log caveat is documented above, not hidden by modifying it.

## Decisions needed before the first window

* Sanitized real portal reply and approved portal access/claim method; verified
  SSH host-key handoff and allocation expiry semantics.
* Grid terms: commercial serving/resale, who pays, billing responsibility, and
  whether overlapping nodes for handoff are allowed. Multi-account/previous
  six-account plans are deferred entirely until terms permit them.
* Persistent local storage availability, attach time, capacity, and egress charges.
* Existing weight mirror bucket/prefix/region and credentials, if desired.
* Netops token and upstream-key handoff through env/credential manager; resolve
  `CF_TUNNEL_TOKEN` vs `TUNNEL_TOKEN`, `INFER_UPSTREAM_KEY` vs `ORIGIN_KEY`.
* Netops confirmation of infer.belweave.tech → infer-origin.belweave.tech,
  b300-infer ingress, origin protection, SSE timeouts, replica readiness, and a
  tested admission/drain endpoint. Pin/checksum/architecture fixes to the helper.
* Target prompt/output lengths, thinking mode, TTFT/TPOT targets, prices, billing
  units, and a real benchmark before capacity or profit claims.

## Sources

Primary sources inspected 2026-10-31:

1. [1017 model card](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017)
2. [Raw config](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/raw/main/config.json)
3. [File list](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/tree/main) and [blob metadata API](https://huggingface.co/api/models/deepseek-ai/DeepSeek-V4.1-Flash-1017?blobs=true)
4. [Full MIT LICENSE](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/raw/main/LICENSE)
5. [Family card: backbone, active counts, Engram, CSA2](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/raw/main/README.md)
6. [Official NVIDIA NVFP4 card](https://huggingface.co/nvidia/DeepSeek-V4.1-Flash-1017-NVFP4) and [file metadata](https://huggingface.co/api/models/nvidia/DeepSeek-V4.1-Flash-1017-NVFP4?blobs=true)
7. [SGLang V4.1 model cookbook](https://docs.sglang.io/cookbook/autoregressive/DeepSeek/DeepSeek-V4_1.md): preview image, B300, native parsers, DSpark, automatic backends
8. [SGLang installation/CUDA support](https://docs.sglang.io/docs/get-started/install)
9. [vLLM v0.32.0 release and exact image tags](https://github.com/vllm-project/vllm/releases/tag/v0.32.0) and [model registry at that tag](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/model_executor/models/registry.py)
10. [vLLM Engram](https://docs.vllm.ai/en/latest/features/engram/) and [V4.1 architecture overview in vLLM Ascend docs](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/DeepSeek-V4.1-Flash.html) (Ascend launch flags are not copied into CUDA presets)
11. [NVIDIA model-specific SGLang NIM guide](https://docs.nvidia.com/nim/large-language-models/latest/get-started/advanced/get-started-deepseek-v4-1-flash-1017.html)
12. [HF CLI/download](https://huggingface.co/docs/huggingface_hub/en/guides/cli) and [HF Xet environment settings](https://huggingface.co/docs/huggingface_hub/en/package_reference/environment_variables)
13. [s5cmd parallel copy documentation](https://github.com/peak/s5cmd)
14. [Cloudflare tunnel run parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/) and [tunnel tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/)
15. [DeepSeek October 17 release announcement](https://api-docs.deepseek.com/news/news261017/) and [deepseek-recipe](https://github.com/deepseek-ai/deepseek-recipe)

# b300-inference

A single-node inference kit for a four-hour, 8× NVIDIA B300 rental. A Mac watches
one national compute grid account, accepts a normalized SSH claim, uploads the
kit, and retrieves benchmark artifacts. The kit is versioned in a private GitHub
repository. No provider integration, purchase, tunnel connection, or deployment
has been performed by this kit-building session.

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

Before a real rental, prepare Linux (amd64 or arm64 with matching engine image) with Docker, NVIDIA Container Toolkit,
Python ≥3.10, `requirements-node.txt`, `rsync`, `curl`, `flock`, `setsid`, `timeout`,
and `zstd`, plus optionally `s5cmd`. Stage all runtime dependencies and compiled
kernel assets before using an offline node; the kit does not install packages there.
The kit checks Docker/toolkit rather than installing or
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
3. Netops provisions mode-600 `~/.config/b300/node.env` on the Mac. The watcher
   copies it opaquely with `scp -p` and remote `chmod 600`; it never reads or sources
   its contents on the Mac. Set `MAC_NODE_ENV_FILE` to change the source path. Node
   bootstrap sources `NODE_ENV_FILE` (default `~/node.env`) if present. `client.env`
   stays on the Mac and is not used by this kit. Other credentials can come from
   the environment. Never put secrets in templates, the repo, argv, or the plist.
   `.env.example` lists names only; do not source it wholesale.
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
teardown: `RUN_ID=<id> ./teardown.sh`; it also loads the private node env for metrics.
An independent node-local timer survives SSH disconnect and begins teardown at
`WINDOW_END - 600`; a reboot does not preserve that timer. Provider expiry remains
the ultimate cutoff. This kit stops workloads; it does not release a billable
allocation or control the grid's billing clock.

The launchd plist is a template only: edit absolute paths, create `logs/`, and
arrange runtime credentials through a credential-manager wrapper before installing
it yourself. Do not put secrets in launchd `EnvironmentVariables`.

## Network modes and handoff

```text
infer.belweave.tech Worker (client keys, limits, between-window 503)
                |
infer-origin.belweave.tech / named tunnel b300-infer
                |
   NETWORK_MODE=cloudflared        NETWORK_MODE=ssh, SSH_DIRECTION=forward
   node cloudflared (outbound)     Netops relay cloudflared -> relay local port
                |                                      |
                |                         relay dials node SSH :22 (-L)
                +---------------------+----------------+
                                      |
                           node 127.0.0.1:8000 + API key
                                      |
                                   8 × B300
```

`NETWORK_MODE=auto` is the default. From the node it makes concurrent HTTPS HEAD
probes with short timeouts to Cloudflare's public API, the pinned GitHub release
asset (following its asset-host redirect), and Hugging Face. All pass selects
`cloudflared`; any failure selects `ssh`. Status records the requested/selected
mode and each result. These probes test HTTPS reachability, not tunnel TCP/UDP
7844 connectivity, registry access, or sustained download throughput. Explicit
`cloudflared` and `ssh` skip the probes. Force `ssh` if the grid only permits
inbound SSH. The old `TUNNEL` selector is replaced by `NETWORK_MODE`.

In SSH mode the watcher uses pushed images and weights (or an existing volume),
not outbound node downloads. The node only serves loopback and reports `ready`;
the **relay dials into it** with `-L`. Only the claim's inbound SSH port is required. No
Docker public ports are published. Keep the node's host/provider ingress policy
at SSH-only, including blocking distributed engine/NCCL ports from outside.

[docs/relay-contract.md](docs/relay-contract.md) is the handoff specification for
Netops. Its existing `relay.sh`, owned by Netops in infer-gateway, is external:
configure `RELAY_SH`, optionally `RELAY_HOST`, and existing SSH key paths. The
watcher calls its exact `up`, `down`, and `push-weights` interfaces. A stable claim
ID gives the same relay identity on retries. `up` is called only after node
readiness; `down` after the node's drain/stop state, or on expiry/error cleanup.
Two overlapping windows get two relay ports/replicas on the same tunnel. The
updated helper accepts the claim SSH port, chooses an unused port in 18000–18999,
checks cloudflared 2026.10.7, and self-tears-down at `--expires`. The wrapper records
the selected port/expiry in `runs/<id>/relay.status.json`. Netops still needs to
qualify health-aware withdrawal, simultaneous reservations, and retry supervision.

For nodes allowing outbound SSH but not HTTPS, `SSH_DIRECTION=reverse` enables
`scripts/reverse-tunnel.sh` with loopback `-R`, keepalives and reconnect. This is
an optional transport, not a duplicate of Netops' relay helper. The draft `up`
only supports forward mode, so Netops must provision reverse-mode relay cloudflared
separately. The watcher never calls forward `up` against a reverse listener.

The updated node helper is preserved **byte-for-byte** and tested against SHA256
`845edd39cb7c91bf5dbcff06b09516833aafbd893ec9ddc42710cbd140fca498`.
It pins cloudflared **2026.10.7**, verifies amd64/arm64 checksums and consumes
`CF_TUNNEL_TOKEN` directly. Bootstrap performs no token-name mapping and calls it
only after health, auth/readiness, and chat smoke pass. It captures the helper's
printed PID. The helper disables xtrace, exports `TUNNEL_TOKEN` then unsets `CF_TUNNEL_TOKEN`
in its own process; execute it instead of sourcing it. Bootstrap's environment
is unaffected. It reuses the verified binary, or downloads/checks `.new` and
renames it over `/tmp/cloudflared`, avoiding text-file-busy during handoff.
It appends to the raw shared `/tmp/cloudflared.log`. Bootstrap records a starting
file identity/offset; teardown filters only subsequent bytes, skipping collection
when the boundary is missing/invalid. Concurrent replica output may interleave.
Long token-shaped values are masked even after rotation, and archive members are
filtered again. See the vendor note for the remaining shared-path caveats. A live PID alone is not proof of tunnel registration.

Both engines serve unauthenticated `GET /health` on **127.0.0.1:8000** despite
`--api-key`; `/v1/models` is protected. Bootstrap checks health without auth,
requires 401 for unauthenticated model listing, verifies authenticated model
listing, then requests a real completion. [Source evidence and test scope](docs/health-contract.md).
Use `INFER_UPSTREAM_KEY` in env. `ORIGIN_KEY` is a deprecated fallback, warns once
per process family when used, and is scheduled for removal next release.
The full kit sends engine secrets through Docker stdin, keeping them out of Docker
configuration. vLLM uses `VLLM_API_KEY` internally; SGLang has no verified native
env/file option and retains a narrow inner-process `--api-key` fallback, visible
in `ps`/`/proc`. SGLang’s inspected startup logger does not mask this field. Raw
`docker logs` could otherwise retain it: the kit sets `--log-driver none` and
filters engine stdout/stderr through `redact.py` before disk. Teardown re-filters
all diagnostic archive members. The short live-node recipe instead uses name-only
`-e` (visible in Docker inspect) and in-container redaction.
[Auth source evidence and residual exposure](docs/auth-contract.md).
[Interactive live-node instructions](docs/live-node.md).

Teardown collects diagnostics, optionally invokes a tested `DRAIN_HOOK` to stop
admission, and waits for running **and queued** request gauges to be zero for three
samples, bounded by `DRAIN_SECONDS` (120). Unknown metrics never mean zero. No
universal engine drain endpoint is fabricated. At the bound, remaining work may
be cut off. In direct mode the default is engine stop then immediate connector
stop; `REPLACEMENT_READY=1` disconnects the old node connector first. In relay
forward mode the watcher calls down after drain/stop, leaving a possible gap up
to `POLL_SECONDS` plus command latency. See the contract for reverse-mode limits.

`GATEWAY=litellm` remains optional at 127.0.0.1:4000; the public tunnel still uses
the engine at :8000. Offline gateway use requires its image already installed.
Changing the public path to go through LiteLLM needs a revised Netops contract.

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

The default SGLang capacity settings match the finalized max-TPS run on 8× B300.
The vLLM preset remains a separate experiment. See [the live launch](docs/live-node.md)
and [model naming, gateway mapping and public listing hold](docs/model-naming.md).

| Setting | SGLang | vLLM |
|---|---|---|
| Parallelism | `--tp 8` | `--tensor-parallel-size 8` |
| Capacity | `--max-running-requests 128` | `--max-num-seqs 64` |
| Prefill | `--chunked-prefill-size 8192` | `--enable-chunked-prefill --max-num-batched-tokens 4096` |
| Graph ceiling | `--cuda-graph-max-bs-decode 128` | `--compilation-config '{"max_cudagraph_capture_size":64}'` |
| Memory/context | `--mem-fraction-static 0.85 --context-length 409600` | `--gpu-memory-utilization 0.80 --max-model-len 409600` |
| Auth/network | loopback :8000; inner argv fallback | loopback :8000; `VLLM_API_KEY` env |
| Native chat | `--reasoning-parser auto --tool-call-parser auto` | `--tokenizer-mode deepseek_v41 --reasoning-parser deepseek_v41 --tool-call-parser deepseek_v41 --enable-auto-tool-choice` |

`PROFILE=longctx` is the default; both engine containers add `--cap-add SYS_NICE`.
`PROFILE=throughput` selects `config/throughput.env`, preserving the old 32,768
context / 256 running / 256 graph ceiling. Explicit exported settings override
preset defaults. Neither preset is a measured capacity guarantee.

The [V4.1 cookbook's long-context section](https://docs.sglang.io/cookbook/autoregressive/DeepSeek/DeepSeek-V4_1.md)
emphasizes prefix reuse, KV offload and session affinity, with settings tied to
specific hardware/topology/engine images. Its static examples of 16K/32K prefill
and 0.70–0.85 memory fractions are AMD configurations, not a verified match for
this eight-B300 409,600-context cell. The live SGLang run uses prefill **8192** and memory **0.85**; do not transplant AMD/offload flags. A long
context cap does not reserve enough KV cache for 128 full-length prompts. Benchmark
actual prompt lengths, cache reuse and memory before tuning or selling capacity.

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
ceilings and capacity together; the existing client sweep through 1024 streams
now queues behind a **128-request** SGLang engine cap. A 1M architectural context does
not mean simultaneous full-length prompts fit. The default per-request cap is
409,600 tokens, with prompt plus output constrained by engine memory.

## Weight delivery and window budget

`WEIGHTS_SOURCE=hf|mirror|push|volume` chooses transport; `mirror` replaces `s3`.
All modes verify against the **committed** file list in `docs/model-metadata.json`:
SHA256/LFS plus size for weights and other LFS files; Git blob SHA1 plus size for
small repo files. No node-side metadata fetch is required, even for mirror mode.
Do not trust a manifest shipped alongside the pushed weights in place of this
committed list. Both fp8 and nvfp4 variants have immutable revisions and file hashes.

With egress, prefer staged local persistent NVMe, then a nearby S3/R2 mirror, then
HF Xet. Without egress, use an existing verified volume or `push`. Preserve the
whole snapshot layout, including native encoding/tokenizer files and LICENSE.
HF uses resumable `snapshot_download(max_workers=16)` with
`HF_XET_HIGH_PERFORMANCE=1` (the same worker control as `hf download --max-workers`).
Current Hub uses hf_xet; legacy hf_transfer is deprecated. Mirror mode uses
s5cmd with bounded file/multipart parallelism. Verified files are skipped; a
partially copied S3 shard restarts. No mirror or volume is created automatically.

`scripts/prestage.sh` downloads once to `STAGING_DIR/<variant>/weights` and exports
an image under `STAGING_DIR/<variant>/images`. Set `MODEL_VARIANT=fp8` (default,
DeepSeek mixed FP8/FP4 base) or `nvfp4` (official NVIDIA variant). It selects the
matching pinned revision automatically. `STAGING_PLATFORM=linux/amd64` prevents
accidentally exporting a Mac-native ARM image; set linux/arm64 for an ARM node.
The real prestage workflow is implemented but **was not run**; only its dry run
was executed. It uses `docker save IMAGE | zstd -T0 -3`, records archive SHA256/
size, immutable image config ID, platform and registry RepoDigests in `engine.json`.
`-T0` intentionally uses available staging CPUs; budget resources on the staging host.

Push mode calls external `relay.sh push-weights` when configured; otherwise it
uses disjoint file lists across `PUSH_WORKERS` parallel rsync `--partial` streams.
The node hashes all files before launch. Image transfer uses resumable rsync;
the node checks the archive before `zstd -dc | docker load`, then verifies the
loaded image config SHA256/platform and launches that immutable ID. Docker save/
load may drop registry RepoDigests: the verified ID is the image config digest,
not a false claim that a distribution manifest digest survived the transfer.
Stage image metadata over the same trusted SSH channel as the archive. Pushed
image archives and runs/<id>/incoming are excluded from teardown diagnostics.

For a remote relay, weights must exist under `RELAY_STAGING_DIR/<variant>/weights`
there; the image archive is currently pushed from the Mac staging directory.
No 510 GB Mac-to-relay staging hop is silently performed. `IMAGE_SOURCE=push|pull`
defaults to push when weights are pushed; SSH mode requires push images. The
watcher automatically switches an offline auto choice to push; direct node
bootstrap fails clearly if no suitable staged assets were supplied. No engine
fallback download is attempted. Stage runtime dependencies/kernel caches too.

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
25–60 min; at 1 Gbit/s allow roughly 110–140 min.
For push, the relay/staging host **upload** rate is the bottleneck: the base
checkpoint alone is about 68–70 min at 1 Gbit/s or 6.8–7 min at 10 Gbit/s; NVFP4
is about 57 min at 1 Gbit/s. A nearby relay or NVFP4 can materially shorten staging.
These are budgets, not provider
promises. The long-context profile admits fewer concurrent requests (128 for SGLang versus
256); long prefills, cache pressure and compilation may change these allowances.
The 409,600 cap is not a measured speed or cold-start claim. Xet/CDN throughput
can be lower. A full 510 GB hash pass alone takes
about 1.7–8.5 min at 5–1 GB/s disk read throughput. Record real phase times before
committing to an operating margin. Container pull and weight pull run concurrently,
so their wall-time contribution is approximately their maximum, not their sum.

| Phase | Budget (unmeasured) |
|---|---|
| Portal allocation / SSH | 2–5 min; use portal expiry, never restart its clock |
| Preflight + runtime checks | 1–2 min |
| Container pull | 3–10 min, overlapped with weights |
| Weight pull / push | table above; push before bootstrap; volume has no transfer |
| Checksum pass | 2–9 min on suitable local storage |
| Load / compile / CUDA graphs | 8–20 min |
| Health / chat / warmup / tunnel | 1–3 min |
| First-window sweep | reserve 15–35 min, workload dependent; not a promise |
| Serving | remaining time to T−10 min |
| Drain / package / retrieval | final 10 min; timer starts on node even if Mac sleeps |

| Push source / link | Unmeasured full cold-start allowance |
|---|---|
| Mac/relay upload, 1 Gbit/s | 110–150 min (base); 95–135 min (NVFP4) |
| Nearby relay upload, 10 Gbit/s | 25–55 min |
| Nearby relay upload, 25 Gbit/s | 20–45 min |

Push occurs before bootstrap: network copy time is therefore additional to the
node phase timings. Add compressed image bytes / upload bandwidth, checksum,
zstd decompression and Docker import. These ranges are planning allowances, not
measurements; at low upload rates the window can be mostly consumed by transfer.

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
This is a scenario supplied for planning, not a grid quotation. The 128-request
SGLang long-context limit can reduce billable aggregate throughput; do not reuse results
from the optional 256-request throughput profile in these calculations. Throughput =
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

The watcher copies only the provisioned node env file; node entrypoints source
`NODE_ENV_FILE` when present, then use caller-exported values for other settings.
The kit never sources `.env.example`; an empty name is not a secret-file template.

| Names | Default / purpose |
|---|---|
| `HF_TOKEN` | Optional for public model; env-only HF access |
| `CF_TUNNEL_TOKEN` | Node helper or relay runtime env; same named tunnel across windows; no kit mapping |
| `INFER_UPSTREAM_KEY`, `ORIGIN_KEY` | Required origin key; deprecated alias only if primary absent, removed next release |
| `LITELLM_MASTER_KEY`, `LITELLM_IMAGE` | Optional gateway key and explicitly pinned image |
| `NODE_SSH_KEY_PATH`, `NODE_KIT_DIR` | Existing SSH identity path; remote relative kit path `b300-inference` |
| `PROVIDER_API_TOKEN` | Reserved hook credential; no provider calls implemented |
| `ACCOUNT_CONFIG`, `ACCOUNT_NAME`, `PROVIDER_HOOKS` | One nonsensitive account config; default config/account.env |
| `MODEL_VARIANT`, `MODEL`, `MODEL_REVISION` | fp8 / nvfp4 chooses pinned model/revision; overrides must match committed metadata |
| `ENGINE`, `ENGINE_IMAGE` | sglang or vllm; image defaults in config/ |
| `WEIGHTS_SOURCE`, `WEIGHTS_DIR` | hf / mirror / push / volume; /data/weights/<model-repo-name> |
| `WEIGHTS_S3_URI`, `S3_ENDPOINT_URL` | Existing mirror prefix; optional R2/S3-compatible endpoint; no credentials in URLs |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION` | Environment credentials for s5cmd; no credential files shipped |
| `ARTIFACT_S3_URI` | Optional artifact upload prefix; 90-second upload bound, local archive retained on failure |
| `PROFILE` | longctx (SGLang 409600/128/128; vLLM 409600/64/64) or throughput (32768/256/256); context/running/graph |
| `MAC_NODE_ENV_FILE`, `NODE_ENV_FILE` | Mac ~/.config/b300/node.env; node ~/node.env; paths only, opaque copy + chmod 600 |
| `RUN_ID`, `WINDOW_END`, `WINDOW_MINUTES` | Run ID; actual expiry epoch preferred; fallback 240 min from node bootstrap |
| `PORT`, `GATEWAY_PORT`, `GATEWAY` | 8000 (required contract), 4000, none |
| `NETWORK_MODE`, `SSH_DIRECTION` | auto / cloudflared / ssh; forward / reverse |
| `STAGING_DIR`, `STAGING_PLATFORM`, `IMAGE_SOURCE` | repo staging/ (ignored); linux/amd64; pull or push |
| `PUSH_DIR`, `PUSH_WORKERS` | Node incoming image directory (watcher sets it); 4 file workers |
| `RELAY_SH`, `RELAY_HOST`, `RELAY_STATE` | External Netops executable; optional remote relay; helper's state dir |
| `RELAY_SSH_KEY_PATH`, `RELAY_NODE_SSH_KEY_PATH`, `RELAY_STAGING_DIR` | Existing Mac→relay key, relay→node key, and relay weight staging root |
| `REVERSE_RELAY_TARGET`, `REVERSE_RELAY_PORT`, `REVERSE_SSH_KEY_PATH` | Opt-in node→relay transport; existing node key/agent; Netops must provision its connector |
| `DRAIN_SECONDS`, `DRAIN_HOOK`, `REPLACEMENT_READY` | 120; optional executable; 0 unless replacement independently verified |
| `FIRST_WINDOW` | 1; benchmark reserved once by local atomic marker |
| `TARGET_TTFT_MS`, `TARGET_TPOT_MS` | 2000, 50; illustrative targets, not promised SLA |
| `CONCURRENCY` | 16,32,64,128,256,512,1024 |
| `OUTPUT_TOKENS`, `PROMPT_REPEATS`, `REQUESTS_PER_STREAM`, `REQUEST_TIMEOUT_SECONDS` | 256, 64, 2, 180; synthetic workload controls |
| `MAX_RUNNING_REQUESTS`, `CHUNKED_PREFILL_SIZE`, `CUDA_GRAPH_MAX_BS` | SGLang: 128, 8192, 128; vLLM: 64, 4096, 64 |
| `MEM_FRACTION`, `CONTEXT_LENGTH`, `PARALLELISM`, `SPECULATION` | SGLang .85 (vLLM .80), 409600, tp / dp_ep, none / dspark |
| `ENGINE_EXTRA_ARGS_JSON` | JSON argv string array, no shell evaluation; do not include secrets |
| `MIN_FREE_GB`, `HEALTH_TIMEOUT_SECONDS` | 650 budget minus existing shards; 2400 seconds, also bounded by window |
| `HF_MAX_WORKERS`, `HF_XET_HIGH_PERFORMANCE`, `HASH_WORKERS` | 16, 1, 4 |
| `S3_WORKERS`, `S3_FILE_WORKERS`, `S3_PART_CONCURRENCY` | 32, 4, 8; bounded parallelism |
| `WATCH_INTERVAL_SECONDS`, `POLL_SECONDS`, `DRY_RUN` | 60, 20, 0 |

## Files and local verification

* `bootstrap.sh`, `loadtest.sh`, `teardown.sh`: node lifecycle and Mac `--claim` entry.
* `scripts/`: atomic status, checkpoint verification, argv generation, in-memory
  secret transport, authenticated HTTP, smoke, drain/deadline, redaction, benchmark, lint.
* `config/`: two long-context engine defaults, optional throughput preset, LiteLLM config, one-account template.
* `claim/`: parser stub, normalized claim validator, fixture placeholder, contract.
* `watcher/`: hooks, single-account loop, SSH deployment, runtime payload, notifications,
  detached benchmark launcher, and uninstalled launchd template.
* `vendor/infer-gateway/`: verbatim Netops script and provenance note.
* `tests/`: claim validation, offline dry runs, native flags/binding, secret redaction,
  status history, fragmented SSE, network decisions, exact vendor hash, image
  integrity, exact relay argv, and mocked watcher push/ready/up/drain/down ordering.
* `docs/live-node.md`: paste-ready blocks for an interactive SSH session.
* `docs/relay-contract.md`, `docs/health-contract.md`, `docs/auth-contract.md`: operational contracts and source evidence.
* `docs/model-metadata.json`: public HF metadata and per-file checksums; `docs/planning.html`: transfer/cost chart.
* `.env.example`, `.gitignore`, `requirements-node.txt`, `Makefile`, this README.

`make lint` runs shellcheck and bash -n on every shell script, including vendor,
and py_compile on Python files. Tests use no GPU/provider/tunnel. `.venv`, logs,
runs, archives, `node.env`, `client.env`, secret env files, and generated status
are ignored. The node env remains a mode-600 runtime file outside the repo; the
Docker stdin transport avoids metadata storage. Fake-key tests cover engine logs
and teardown archives. The vendor helper's raw-log/shared-path caveat remains;
it is preserved unchanged.

## Decisions needed before the first window

* Sanitized real portal reply and approved portal access/claim method; verified
  SSH host-key handoff and allocation expiry semantics.
* Grid terms: commercial serving/resale, who pays, billing responsibility, and
  whether overlapping nodes for handoff are allowed. Multi-account/previous
  six-account plans are deferred entirely until terms permit them.
* Persistent local storage availability, attach time, capacity, and egress charges.
* Existing weight mirror bucket/prefix/region and credentials, if desired.
* Netops token and upstream-key handoff through env/credential manager; use
  canonical `CF_TUNNEL_TOKEN` and `INFER_UPSTREAM_KEY`; retire the deprecated alias.
* Netops confirmation of infer.belweave.tech → infer-origin.belweave.tech,
  b300-infer ingress, origin protection, SSE timeouts, replica readiness, and a
  tested admission/drain endpoint. Updated relay version checks, free-port selection,
  expiry, custom SSH ports and rsync compatibility are implemented by Netops;
  public handoff, concurrent reservations and retry supervision need qualification.
* Relay host/executable path, existing key paths, staged storage, and upload rate;
  offline Docker/kernel dependencies. Reverse mode needs Netops connector support.
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

16. [SGLang auth source](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/utils/auth.py) and [main HTTP routes](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/entrypoints/http_server.py)
17. [vLLM v0.32.0 health route](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/entrypoints/serve/instrumentator/health.py) and [auth middleware](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/entrypoints/serve/middleware/authenticate.py)
